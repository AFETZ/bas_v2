#!/usr/bin/env python3
"""One-host BAS runtime: explicit configuration, owned lifecycle, raw event views."""
import argparse
import datetime
import hashlib
import heapq
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
import time

ROOT = Path(__file__).resolve().parent
LOCK = Path('/tmp/bas-paper-runtime.lock')
SHAPE = {
    'environment': {'engine', 'scene'}, 'drone': {'autopilot', 'count'},
    'propagation': {'model'}, 'network': {'engine', 'topology'},
    'experiment': {'policy', 'duration_s', 'seed', 'run'},
    'runtime': {'clock', 'image', 'land_timeout_s', 'sionna_log_level', 'event_logging', 'capture_storage', 'rt_stage_trace'},
    'flows': {'channels', 'payload_attitude_interval_s', 'payload_load'},
    'interference': {'sources'},
    'hitl': {'mode', 'request', 'request_sha256', 'profiles_sha256'},
}
PROPAGATION_FIELDS = {
    'geometry_logdistance': {'model'},
    'sionna': {'model', 'radio_config', 'ground_material', 'building_material', 'thickness_m'},
    'hybrid': {'model', 'radio_config', 'ground_material', 'building_material', 'thickness_m'},
}
MILESTONES = {'service_started', 'phase_requested', 'takeoff_observed',
              'waypoint_reached_telemetry', 'barrier_released', 'experiment_failed',
              'stop_requested', 'urban_measurement_window_closed',
              'urban_laboratory_cleanup_started', 'landing_completed'}


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError('run ID must contain 1..80 letters, digits, underscores or hyphens')
    return value


def unique_json_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate configuration field: {key}')
        result[key] = value
    return result


def reject_json_constant(value):
    raise ValueError(f'non-finite JSON value is not allowed: {value}')


def validate_duration(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 1e-9 <= value <= 86400
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('experiment.duration_s must be a finite number from 1 ns to 86400 s')
    return value


def validate_capture_storage(config):
    duration = validate_duration(config['experiment']['duration_s'])
    runtime = config['runtime']
    storage = runtime.get('capture_storage', 'tmpfs')
    if storage not in ('tmpfs', 'disk'):
        raise ValueError('runtime.capture_storage must be tmpfs or disk')
    if duration > 2400 and storage != 'disk':
        raise ValueError('duration beyond 2400 s requires runtime.capture_storage=disk; long tmpfs capture volume is not qualified')


def runtime_duration_limits(duration, land_timeout_s, uav_count, channels, *, mode='relay_experiment', parameter_snapshots=False,
                            radio_bench=False):
    """Lifetimes only: existing readiness/flight/stop deadlines remain unchanged."""
    validate_duration(duration)
    if radio_bench:
        if mode != 'latency_diagnostic' or type(uav_count) is not int or not 1 <= uav_count <= 5 or channels != ['control', 'payload']:
            raise ValueError('radio_bench lifetime requires latency_diagnostic and both channels for 1..5 UAVs')
        # Existing native_radio_five_uav_scenario.py bounds: heartbeat readiness,
        # 30 s warmup, payload REQUEST_MESSAGE, sequential/parallel operations,
        # all three retry candidates (including waits) and at most 20 PING/UAV.
        budgets = dict(native_readiness_s=180, heartbeat_readiness_s=90, warmup_s=30,
            payload_requests_s=3 * uav_count, sequential_operations_s=10 * uav_count,
            parallel_operations_s=10, retry_characterization_s=18 * uav_count,
            ping_s=20 * uav_count, observation_s=duration,
            no_bypass_readiness_and_draining_s=30, no_bypass_observation_s=10.5,
            native_stop_wait_s=120, post_capture_wait_s=2, process_term_grace_s=5)
        native = max(2400, math.ceil(sum(budgets.values())))
        before_native = 15 * 2 * uav_count + 240 + 180 + uav_count * 30 * (5 + .2) + 15
        service = max(3600, math.ceil(native + before_native))
        if service > 86400:
            raise ValueError('radio_bench observation plus startup/diagnostics exceeds the existing 86400 s external-adapter lifetime')
        return dict(mode=mode, radio_bench=True, native_duration_s=native, service_duration_s=service,
                    native_budgets=budgets, basis='declared host window plus unchanged bounded non-flight diagnostics')
    if mode != 'relay_experiment':
        return dict(mode=mode, native_duration_s=2400, service_duration_s=3600,
                    basis='legacy product/diagnostic/operator bounds; no extension from measurement duration')
    if (type(uav_count) is not int or not 1 <= uav_count <= 5
            or channels not in (['control'], ['control', 'payload'])
            or type(land_timeout_s) not in (int, float) or not math.isfinite(land_timeout_s) or land_timeout_s <= 0):
        raise ValueError('invalid lifecycle roster, channels or landing budget')
    # Source: runner readiness loops; relay_experiment argparse defaults and
    # prepare/capture_parameters/measure; runner post-scenario stop/TERM grace.
    native_budgets = dict(native_readiness_s=180, flight_ready_s=120,
        guided_per_uav_s=30, arm_per_uav_s=120, takeoff_per_uav_s=90,
        all_uav_prepare_s=uav_count * (30 + 120 + 90),
        parameter_capture_before_after_s=2 * (600 + 3) if parameter_snapshots else 0,
        measurement_wall_watchdog_s=duration * 1.1 + 10, landing_s=land_timeout_s,
        native_stop_wait_s=120, post_capture_wait_s=2, process_term_grace_s=5)
    native_total = (180 + 120 + native_budgets['all_uav_prepare_s'] +
        native_budgets['parameter_capture_before_after_s'] + native_budgets['measurement_wall_watchdog_s'] +
        land_timeout_s + 120 + 2 + 5)
    # UAV services start before Gazebo/native readiness, unlike native ns-3.
    before_native = dict(endpoint_readiness_s=15 * len(channels) * uav_count,
        gazebo_odometry_readiness_s=240, stack_health_s=180,
        ros_odometry_discovery_s=uav_count * 30 * (5 + .2), ros_topic_export_s=15)
    native = max(2400, math.ceil(native_total))
    return dict(mode=mode, declared_measurement_s=duration, native_duration_s=native,
        service_duration_s=max(3600, math.ceil(native + sum(before_native.values()))),
        native_budgets=native_budgets, service_before_native_budgets=before_native,
        basis='max(legacy cap, declared wall watchdog + unchanged bounded lifecycle budgets)')


def _interference_input(config, root, radio_profile=None):
    """Validate exact source bytes with the existing native schedule producer."""
    name = config.get('interference', {}).get('sources')
    if name is None:
        return None, None
    if config['propagation']['model'] not in ('sionna', 'hybrid'):
        raise ValueError('interference.sources requires propagation.model=sionna or hybrid')
    if not isinstance(name, str) or not name or ':' in name:
        raise ValueError('interference.sources must be null or a workspace-relative YAML path')
    relative = Path(name.replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() not in ('.yaml', '.yml'):
        raise ValueError('interference.sources must be a workspace-relative YAML path without traversal')
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError('interference.sources must stay inside the workspace') from None
    raw = path.read_bytes()
    import yaml
    from scripts.product.prepare_native_sources import UniqueSafeLoader, prepare
    from scripts.product.prepare_sionna_scene import ITU_FREQUENCIES_GHZ
    try:
        schedule = prepare(yaml.load(raw.decode('utf-8-sig'), Loader=UniqueSafeLoader))
    except yaml.YAMLError as error:
        raise ValueError(f'interference.sources contains invalid YAML: {error}') from error
    for segment in schedule['segments']:
        frequency_ghz = segment['center_hz'] / 1e9
        for field in ('ground_material', 'building_material'):
            material = config['propagation'][field]
            if material not in ITU_FREQUENCIES_GHZ or not any(
                    low <= frequency_ghz <= high for low, high in ITU_FREQUENCIES_GHZ[material]):
                raise ValueError(f'interference source {segment["id"]} frequency is outside {material} ITU validity')
    if radio_profile is not None and radio_profile['sionna']['execution_mode'] == 'worker_pool':
        signatures = {(segment['center_hz'], segment['pattern']) for segment in schedule['segments']}
        needed = 1 + len(signatures)  # Desired full mesh plus fixed emitter RF roles.
        available = radio_profile['sionna']['worker_count']
        if needed > available:
            raise ValueError(f'worker_pool needs {needed} fixed RF roles for these sources; worker_count={available}')
    return path, raw


def _hitl_input(config, root):
    """Resolve one external dual-channel owner using the existing bench adapter contract."""
    hitl = config.get('hitl')
    if hitl is None:
        return None
    if (not isinstance(hitl, dict) or set(hitl) - SHAPE['hitl'] or not {'mode', 'request'} <= set(hitl)
            or hitl.get('mode') != 'radio_bench'):
        raise ValueError('hitl must be null or {mode: radio_bench, request: workspace-relative JSON path}')
    if config['experiment']['policy'] != 'off':
        raise ValueError('hitl radio_bench requires experiment.policy=off; external controllers do not execute the SITL flight mission')
    if config['flows']['channels'] != ['control', 'payload'] or config['flows'].get('payload_load') is not None:
        raise ValueError('hitl radio_bench requires both UART channels and payload_load=null')
    if config['flows']['payload_attitude_interval_s'] != .2 or config['runtime']['land_timeout_s'] != 120:
        raise ValueError('radio_bench does not use SITL ATTITUDE/landing settings; retain defaults 0.2 s and 120 s')
    name = hitl['request']
    if not isinstance(name, str) or not name or ':' in name:
        raise ValueError('hitl.request must be a workspace-relative JSON path')
    relative = Path(name.replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() != '.json':
        raise ValueError('hitl.request must be a workspace-relative JSON path without traversal')
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError('hitl.request must stay inside the workspace') from None
    request_bytes = path.read_bytes()
    profiles_bytes = (root / 'network/config/hitl_reference_devices.json').read_bytes()
    for key, raw in (('request_sha256', request_bytes), ('profiles_sha256', profiles_bytes)):
        if key in hitl and (not isinstance(hitl[key], str) or not re.fullmatch('[0-9a-f]{64}', hitl[key])
                            or hitl[key] != hashlib.sha256(raw).hexdigest()):
            raise ValueError(f'hitl.{key} differs from the validated input; reload the intended request/profiles')
    def decode(raw):
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_json_fields,
                          parse_constant=reject_json_constant)
    from scripts.product.test_radio_hitl import resolve
    bundle = resolve(decode(request_bytes), decode(profiles_bytes), native_uav_count=config['drone']['count'])
    bundle['reference_profiles_sha256'] = hashlib.sha256(profiles_bytes).hexdigest()
    runtime_duration_limits(config['experiment']['duration_s'], config['runtime']['land_timeout_s'],
                            config['drone']['count'], config['flows']['channels'],
                            mode='latency_diagnostic', radio_bench=True)
    return path, request_bytes, profiles_bytes, bundle


def load_config(path, root=ROOT):
    config = json.loads(path.read_text(encoding='utf-8-sig'),
                        object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
    if isinstance(config, dict):
        config.setdefault('interference', {'sources': None})
        config.setdefault('hitl', None)
    if not isinstance(config, dict) or set(config) != set(SHAPE):
        raise ValueError('runtime config has missing or unknown sections')
    if isinstance(config['runtime'], dict):
        config['runtime'].setdefault('sionna_log_level', 'info')
        config['runtime'].setdefault('event_logging', 'metrics_only')
        config['runtime'].setdefault('capture_storage', 'tmpfs')
        config['runtime'].setdefault('rt_stage_trace', False)
    if isinstance(config['flows'], dict):
        config['flows'].setdefault('payload_load', None)
    for section, keys in SHAPE.items():
        if section == 'hitl':
            if config[section] is not None and (not isinstance(config[section], dict)
                    or set(config[section]) - keys or not {'mode', 'request'} <= set(config[section])):
                raise ValueError('hitl requires mode/request and optional validated SHA-256 pins')
            continue
        if section == 'propagation' and isinstance(config[section], dict):
            model = config[section].get('model')
            if not isinstance(model, str) or model not in PROPAGATION_FIELDS:
                raise ValueError('propagation.model must be geometry_logdistance, sionna or qualified hybrid')
            keys = PROPAGATION_FIELDS[model]
        if not isinstance(config[section], dict) or set(config[section]) != keys:
            raise ValueError(f'expected {section} fields: {sorted(keys)}')
    supported = [('environment', 'engine', 'gazebo'), ('drone', 'autopilot', 'ardupilot_sitl'),
                 ('network', 'engine', 'ns3'), ('network', 'topology', 'adhoc'),
                 ('runtime', 'clock', 'realtime')]
    for section, key, value in supported:
        if type(config[section][key]) is not type(value) or config[section][key] != value:
            raise ValueError(f'this profile requires {section}.{key}={value}')
    count = config['drone']['count']
    if config['runtime']['sionna_log_level'] not in ('info', 'debug'):
        raise ValueError('runtime.sionna_log_level must be info or debug')
    if config['runtime']['event_logging'] not in ('metrics_only', 'batched_trace'):
        raise ValueError('runtime.event_logging must be metrics_only or batched_trace')
    if type(config['runtime']['rt_stage_trace']) is not bool:
        raise ValueError('runtime.rt_stage_trace must be boolean')
    if config['runtime']['rt_stage_trace'] and config['propagation']['model'] not in ('sionna', 'hybrid'):
        raise ValueError('runtime.rt_stage_trace requires Sionna worker_pool')
    if type(count) is not int or not 1 <= count <= 5:
        raise ValueError('drone.count must be an integer from 1 to 5')
    channels = config['flows']['channels']
    if channels not in (['control'], ['control', 'payload']):
        raise ValueError('flows.channels must be [control] or [control, payload] in that order')
    from network.coordination.payload import normalize_payload_load
    config['flows']['payload_load'] = normalize_payload_load(config['flows']['payload_load'], tuple(range(1, count + 1)))
    if config['flows']['payload_load'] is not None:
        if 'payload' not in channels or config['runtime']['event_logging'] != 'batched_trace':
            raise ValueError('flows.payload_load requires payload channel and runtime.event_logging=batched_trace')
    interval = config['flows']['payload_attitude_interval_s']
    try:
        valid_interval = type(interval) in (int, float) and math.isfinite(interval) and .02 <= interval <= 60
    except OverflowError:
        valid_interval = False
    if not valid_interval:
        raise ValueError('flows.payload_attitude_interval_s must be finite and between .02 and 60 seconds')
    if config['experiment']['policy'] not in ('fixed', 'failover') and not (
            config['experiment']['policy'] == 'off' and config['hitl'] is not None):
        raise ValueError('policy must be fixed or failover; off requires explicit hitl radio_bench')
    for section, key in [('experiment', 'duration_s'), ('runtime', 'land_timeout_s')]:
        value = config[section][key]
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f'{section}.{key} must be finite and positive')
    validate_capture_storage(config)
    for key, minimum in [('seed', 1), ('run', 0)]:
        value = config['experiment'][key]
        if type(value) is not int or value < minimum:
            raise ValueError(f'experiment.{key} must be an integer >= {minimum}')
    image = config['runtime']['image']
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('runtime.image must be an immutable Docker image ID')
    scene_name = config['environment']['scene']
    if not isinstance(scene_name, str) or not scene_name:
        raise ValueError('environment.scene must be a nonempty path string')
    root = root.resolve()
    scene = (root / scene_name.replace('\\', '/')).resolve()
    try:
        scene.relative_to(root)
    except ValueError:
        raise ValueError('environment.scene must stay inside the workspace') from None
    if not scene.is_file():
        raise ValueError(f'scene does not exist: {scene}')
    config['environment']['scene'] = scene.relative_to(root).as_posix()
    from scripts.product.urban_mission import UrbanScenario
    scenario = UrbanScenario.load(scene, root=root)
    if set(scenario.raw) & {'physics', 'failsafe'}:
        from scripts.product.urban_mission import EXECUTION_SETTINGS_IMAGE
        if image != EXECUTION_SETTINGS_IMAGE:
            raise ValueError('explicit scene physics/failsafe settings require the pinned qualified runtime image')
    if (config['hitl'] is not None
            and scenario.raw.get('failsafe', {}).get('gcs_action', 'disabled') != 'disabled'):
        raise ValueError('GCS failsafe actions require the virtual relay GCS; radio_bench does not configure a physical controller')
    if tuple(sorted(scenario.spawns)) != tuple(range(1, count + 1)):
        raise ValueError('scene UAV roster must match drone.count exactly')
    if scenario.terrain_bundle_path is not None and config['propagation']['model'] not in ('sionna', 'hybrid'):
        raise ValueError('terrain_bundle requires Sionna; flat geometry propagation cannot represent terrain')
    if config['propagation']['model'] in ('sionna', 'hybrid'):
        propagation = config['propagation']
        radio_name = propagation['radio_config']
        if not isinstance(radio_name, str) or not radio_name:
            raise ValueError('propagation.radio_config must be a nonempty path string')
        radio_file = (root / radio_name.replace('\\', '/')).resolve()
        try:
            radio_file.relative_to(root)
        except ValueError:
            raise ValueError('propagation.radio_config must stay inside the workspace') from None
        if not radio_file.is_file():
            raise ValueError('propagation.radio_config does not exist')
        from scripts.product.prepare_urban_gazebo import read_rt_radio_profile
        from scripts.product.prepare_sionna_scene import ITU_FREQUENCIES_GHZ
        raw_scene = scenario.raw
        radio_profile = read_rt_radio_profile(radio_file, raw_scene)
        if (radio_profile['sionna']['execution_mode'] == 'worker_pool'
                and config.get('interference', {}).get('sources') is not None
                and radio_profile['sionna']['receiver_batch_mode'] != 'full_mesh'):
            raise ValueError('worker_pool with directed sources requires receiver_batch_mode=full_mesh')
        if config['runtime']['rt_stage_trace'] and radio_profile['sionna']['execution_mode'] != 'worker_pool':
            raise ValueError('runtime.rt_stage_trace requires Sionna worker_pool')
        if config['propagation']['model'] == 'hybrid':
            from scripts.product.hybrid_policy import validate_radio_profile
            validate_radio_profile(radio_profile)
        thickness = propagation['thickness_m']
        if type(thickness) not in (int, float) or not 0 < thickness <= sys.float_info.max:
            raise ValueError('propagation.thickness_m must be finite and positive')
        frequency = raw_scene['radio']['frequency_hz'] / 1e9
        for field in ('ground_material', 'building_material'):
            material = propagation[field]
            if not isinstance(material, str) or material not in ITU_FREQUENCIES_GHZ or not any(
                    low <= frequency <= high for low, high in ITU_FREQUENCIES_GHZ[material]):
                raise ValueError(f'{field} must be an explicit supported ITU material at the scene frequency')
        if scenario.terrain_manifest is not None and any(
                propagation[field] != scenario.terrain_manifest['materials'][field]
                for field in ('ground_material', 'building_material', 'thickness_m')):
            raise ValueError('RT profile materials/thickness must match the shared terrain bundle')
        propagation['radio_config'] = radio_file.relative_to(root).as_posix()
    source_path, _ = _interference_input(config, root,
        radio_profile if config['propagation']['model'] in ('sionna', 'hybrid') else None)
    if source_path is not None:
        config['interference']['sources'] = source_path.relative_to(root).as_posix()
    hitl_input = _hitl_input(config, root)
    if hitl_input is not None:
        config['hitl']['request'] = hitl_input[0].relative_to(root).as_posix()
        config['hitl'].update(request_sha256=hashlib.sha256(hitl_input[1]).hexdigest(),
                              profiles_sha256=hashlib.sha256(hitl_input[2]).hexdigest())
    return config


def environment(config, run_id, inherited=None):
    validate_capture_storage(config)
    # Do not let an old terminal session silently activate faults, GUI or another backend.
    env = {k: v for k, v in (os.environ if inherited is None else inherited).items()
           if not k.startswith(('BAS_', 'ROS_', 'GZ_', 'IGN_', 'RMW_'))}
    settings = {
        'CONTAINER_IMAGE': config['runtime']['image'], 'SIMULATION_MODE': 'realtime',
        'NATIVE_FIVE_CONTAINER_NAME': 'bas-core-' + run_id,
        'NATIVE_FIVE_RUN_ID': run_id, 'NATIVE_FIVE_SCENARIO': 'urban',
        'NATIVE_URBAN_CONFIG': config['environment']['scene'],
        'NATIVE_PROPAGATION_PROFILE': config['propagation']['model'],
        'NATIVE_COORDINATION_POLICY': config['experiment']['policy'],
        'NATIVE_EXPERIMENT_DURATION_S': config['experiment']['duration_s'],
        'NATIVE_RNG_SEED': config['experiment']['seed'], 'NATIVE_RNG_RUN': config['experiment']['run'],
        'NATIVE_LAND_TIMEOUT_S': config['runtime']['land_timeout_s'],
        'NATIVE_UAV_COUNT': config['drone']['count'], 'NATIVE_FIVE_GUI': 0, 'NATIVE_CAPTURE_IMAGES': 0,
        'NATIVE_CAPTURE_STORAGE': config['runtime'].get('capture_storage', 'tmpfs'),
        'NATIVE_EVENT_LOGGING': config['runtime'].get('event_logging', 'metrics_only'),
        'NATIVE_FIVE_SKIP_BUILD': 1, 'NATIVE_PHYSICS_PROFILE': 'stock',
        'NATIVE_WIFI_TOPOLOGY': 'adhoc', 'NATIVE_WIFI_AGGREGATION': 0,
        'NATIVE_WIFI_EXPLICIT_BAR': 0, 'NATIVE_UART_CHANNELS': ','.join(config['flows']['channels']),
        'NATIVE_PAYLOAD_INTERVAL_S': config['flows']['payload_attitude_interval_s'],
        'NATIVE_PAYLOAD_LOAD_JSON': json.dumps(config['flows'].get('payload_load'), separators=(',', ':'), allow_nan=False),
        'NATIVE_FAULT_PROFILE': 'none', 'NATIVE_EXPERIMENT_PROFILE': 'urban',
        'NATIVE_EXPERIMENT_PILOT': 1, 'NATIVE_PARAMETER_SNAPSHOTS': 0,
        'NATIVE_COMMAND_PERIOD_S': 1, 'NATIVE_TASK_PERIOD_S': 20,
        'NATIVE_SIONNA_LOG_LEVEL': config['runtime'].get('sionna_log_level', 'info'),
        'NATIVE_RT_STAGE_TRACE': int(config['runtime'].get('rt_stage_trace', False)),
    }
    if config['propagation']['model'] in ('sionna', 'hybrid'):
        propagation = config['propagation']
        settings.update(NATIVE_RADIO_CONFIG=propagation['radio_config'],
                        NATIVE_RT_GROUND_MATERIAL=propagation['ground_material'],
                        NATIVE_RT_BUILDING_MATERIAL=propagation['building_material'],
                        NATIVE_RT_THICKNESS_M=propagation['thickness_m'])
    if config.get('hitl') is not None:
        settings.update(NATIVE_EXTERNAL_BUNDLE=f'runs/control/{run_id}/hitl-bundle.json',
                        NATIVE_EXTERNAL_PROFILES=f'runs/control/{run_id}/hitl-reference-profiles.json',
                        NATIVE_LATENCY_MODE=1, NATIVE_FLIGHT_DIAGNOSTIC=0, NATIVE_OPERATOR_SECONDS=0,
                        NATIVE_EXPERIMENT_PILOT=0, NATIVE_FIVE_TIMEOUT_SCALE=1)
    settings['NATIVE_URBAN_OUTPUT'] = f'runs/control/{run_id}/generated'
    env.update({'BAS_' + k: str(v) for k, v in settings.items()})
    return env


def control_dir(run_id):
    return ROOT / 'runs/control' / identifier(run_id)


def raw_dir(run_id):
    return ROOT / 'runs/native-radio-realtime' / identifier(run_id)


def snapshot_inputs(config, folder, env, *, root=None):
    """Freeze the exact input files consumed by both host preparation and Docker."""
    root = ROOT if root is None else Path(root)
    write_json(folder / 'config.json', config)
    scene_bytes = (root / config['environment']['scene']).read_bytes()
    (folder / 'scene.json').write_bytes(scene_bytes)
    env['BAS_NATIVE_URBAN_CONFIG'] = (folder / 'scene.json').relative_to(root).as_posix()
    hashes = dict(scene_sha256=hashlib.sha256(scene_bytes).hexdigest(), radio_sha256=None, sources_sha256=None)
    if config['propagation']['model'] in ('sionna', 'hybrid'):
        radio_bytes = (root / config['propagation']['radio_config']).read_bytes()
        (folder / 'radio.yaml').write_bytes(radio_bytes)
        env['BAS_NATIVE_RADIO_CONFIG'] = (folder / 'radio.yaml').relative_to(root).as_posix()
        hashes['radio_sha256'] = hashlib.sha256(radio_bytes).hexdigest()
    # Source bytes can change after UI validation. Revalidate the exact bytes
    # being frozen and forward only this owned snapshot, never ambient BAS_*.
    env.pop('BAS_NATIVE_SOURCES', None)
    source_path, source_bytes = _interference_input(config, root.resolve())
    if source_path is not None:
        snapshot = folder / 'sources.yaml'
        snapshot.write_bytes(source_bytes)
        env['BAS_NATIVE_SOURCES'] = snapshot.relative_to(root).as_posix()
        hashes['sources_sha256'] = hashlib.sha256(source_bytes).hexdigest()
    env.pop('BAS_NATIVE_EXTERNAL_BUNDLE', None)
    env.pop('BAS_NATIVE_EXTERNAL_PROFILES', None)
    hitl_input = _hitl_input(config, root.resolve())
    if hitl_input is not None:
        _, request_bytes, profiles_bytes, bundle = hitl_input
        (folder / 'hitl-request.json').write_bytes(request_bytes)
        (folder / 'hitl-reference-profiles.json').write_bytes(profiles_bytes)
        path = folder / 'hitl-bundle.json'
        write_json(path, bundle)
        env['BAS_NATIVE_EXTERNAL_BUNDLE'] = path.relative_to(root).as_posix()
        env['BAS_NATIVE_EXTERNAL_PROFILES'] = (folder / 'hitl-reference-profiles.json').relative_to(root).as_posix()
        hashes.update(hitl_request_sha256=hashlib.sha256(request_bytes).hexdigest(),
                      hitl_profiles_sha256=hashlib.sha256(profiles_bytes).hexdigest(),
                      hitl_bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return hashes


def node_state_journal_verification(raw):
    """Check published bytes against the closed shared-writer ledger, without rewriting poses."""
    state_path, audit_path = raw / 'logs/node_state.json', raw / 'logs/node_state.jsonl.writer.json'
    result = dict(status='invalid_position_journal', passed=False)
    try:
        environment = raw / 'environment.txt'
        declared = re.search(r'^position_journal_mode=(.+)$',
            environment.read_text(encoding='utf-8') if environment.exists() else '', re.M)
        if declared and declared.group(1).strip() != 'bounded_async_jsonl_v1':
            raise ValueError('unsupported advertised position journal mode')
        if not declared and not state_path.exists() and not audit_path.exists():
            return dict(status='not_requested_legacy', passed=None)
        state = json.loads(state_path.read_text(encoding='utf-8'),
                           object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
        if not declared and isinstance(state, dict) and 'journal_mode' not in state and not audit_path.exists():
            return dict(status='not_requested_legacy', passed=None)
        audit = json.loads(audit_path.read_text(encoding='utf-8'),
                           object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
        if not isinstance(state, dict) or not isinstance(audit, dict):
            raise ValueError('position state and writer audit must be JSON objects')
        producer, writer = audit.get('producer'), audit.get('writer')
        if not isinstance(producer, dict) or not isinstance(writer, dict):
            raise ValueError('position writer process and thread identity is missing')
        parts = PurePosixPath(audit['path'].replace('\\', '/')).parts if isinstance(audit.get('path'), str) else ()
        checks = dict(
            metadata=(type(audit.get('schema')) is int and audit['schema'] == 1
                and state.get('journal_mode') == audit.get('journal_mode') == 'bounded_async_jsonl_v1'
                and state.get('source') in ('gazebo_odometry', 'ros_odometry')
                and audit.get('source') == state['source'] and isinstance(state.get('session_id'), str)
                and bool(state['session_id']) and audit.get('session_id') == state['session_id']
                and audit.get('status') == 'closed'
                and all(key in audit and audit[key] is None for key in ('producer_error', 'close_error'))),
            closed=(audit.get('closed') is True and audit.get('drained') is True and audit.get('listener_alive') is False
                and all(type(audit.get(key)) is int and audit[key] == 0 for key in ('queue_depth', 'failed', 'rejected', 'error_count'))
                and audit.get('first_exception') is None and audit.get('last_exception') is None
                and audit.get('unwritten_records') == [] and audit.get('in_progress_record') is None
                and audit.get('rejected_record') is None),
            ledger=(all(type(audit.get(key)) is int and audit[key] > 0
                for key in ('accepted', 'written', 'accepted_bytes', 'written_bytes', 'capacity', 'max_record_bytes'))
                and audit.get('accepted') == audit.get('written') and audit.get('accepted_bytes') == audit.get('written_bytes')
                and audit['capacity'] <= 512 and audit['max_record_bytes'] <= 65536
                and type(audit.get('max_queue')) is int and 0 <= audit['max_queue'] <= audit['capacity']),
            identity=(all(type(item.get(key)) is int and item[key] > 0
                for item in (producer, writer) for key in ('pid', 'thread_id', 'native_thread_id'))
                and producer.get('pid') == writer.get('pid') and producer.get('native_thread_id') != writer.get('native_thread_id')
                and '..' not in parts and parts[-3:] == (raw.name, 'logs', 'node_state.jsonl')))
        path = raw / 'logs/node_state.jsonl'
        if not path.resolve().is_relative_to(raw.resolve()):
            raise ValueError('position journal is outside its run')
        digest, count, size, largest = hashlib.sha256(), 0, 0, 0
        with path.open('rb') as stream:
            while line := stream.readline(65537):
                if len(line) > 65536 or not line.endswith(b'\n'):
                    raise ValueError('position journal record is oversized or incomplete')
                digest.update(line); count += 1; size += len(line); largest = max(largest, len(line))
        checks['file'] = (checks['ledger'] and largest <= audit['max_record_bytes']
                          and count == audit.get('written') and size == audit.get('written_bytes')
                          and digest.hexdigest() == audit.get('sha256'))
        result.update(checks=checks, rows=count, bytes=size, sha256=digest.hexdigest(),
                      path='logs/node_state.jsonl', audit_path='logs/node_state.jsonl.writer.json')
        if all(checks.values()):
            result.update(status='verified_closed_position_journal', passed=True)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result['error'] = str(error)
    return result


def payload_journal_verification(raw):
    """Bind the optional async payload writer's closed ledger to its original file."""
    raw = Path(raw)
    metadata_path, path = raw / 'metrics/payload_channel.json', raw / 'payload/logs/scenario_events.jsonl'
    result = dict(status='invalid_payload_journal', passed=False)
    try:
        def read(path):
            return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_json_fields,
                              parse_constant=reject_json_constant)
        environment = raw / 'environment.txt'
        declared = re.search(r'^payload_journal_mode=(.+)$',
            environment.read_text(encoding='utf-8') if environment.exists() else '', re.M)
        metadata = read(metadata_path) if metadata_path.is_file() else {}
        if not isinstance(metadata, dict):
            raise ValueError('payload channel metadata must be an object')
        lineage = metadata.get('application_lineage', {})
        if not isinstance(lineage, dict):
            raise ValueError('payload lineage metadata must be an object')
        raw_mode = None
        if path.is_file():
            with path.open('rb') as stream:
                first = stream.readline(65537)
            if len(first) <= 65536:
                try:
                    row = json.loads(first)
                    raw_mode = row.get('journal_mode') if isinstance(row, dict) else None
                except (ValueError, UnicodeError):
                    pass  # Legacy raw syntax is audited by the packet analyzer.
        if not declared and 'journal_mode' not in lineage and raw_mode is None:
            return dict(status='not_requested_legacy', passed=None)
        if declared and declared.group(1).strip() != 'bounded_async_jsonl_v1':
            raise ValueError('unsupported advertised payload journal mode')
        audit = lineage.get('writer')
        if not isinstance(audit, dict):
            raise ValueError('payload writer audit is missing')
        producer, writer = audit.get('producer'), audit.get('writer')
        if not isinstance(producer, dict) or not isinstance(writer, dict):
            raise ValueError('payload writer process and thread identity is missing')
        parts = PurePosixPath(audit['path'].replace('\\', '/')).parts if isinstance(audit.get('path'), str) else ()
        checks = dict(
            metadata=(type(metadata.get('schema_version')) is int and metadata['schema_version'] == 1
                and metadata.get('channel') == 'payload' and metadata.get('status') == 'closed'
                and metadata.get('close_error') is None
                and lineage.get('journal_mode') == 'bounded_async_jsonl_v1'
                and raw_mode in (None, 'bounded_async_jsonl_v1')
                and lineage.get('event_logging') == 'batched_trace'
                and lineage.get('status') == 'gcs_boundary_trace_closed'
                and lineage.get('path') == 'payload/logs/scenario_events.jsonl'),
            closed=(audit.get('closed') is True and audit.get('drained') is True and audit.get('listener_alive') is False
                and all(type(audit.get(key)) is int and audit[key] == 0 for key in ('queue_depth', 'failed', 'rejected', 'error_count'))
                and audit.get('first_exception') is None and audit.get('last_exception') is None
                and audit.get('unwritten_records') == [] and audit.get('in_progress_record') is None
                and audit.get('rejected_record') is None),
            ledger=(all(type(audit.get(key)) is int and audit[key] > 0
                for key in ('accepted', 'written', 'accepted_bytes', 'written_bytes', 'capacity', 'max_record_bytes'))
                and audit.get('accepted') == audit.get('written') and audit.get('accepted_bytes') == audit.get('written_bytes')
                and audit['capacity'] <= 512 and audit['max_record_bytes'] <= 65536
                and type(audit.get('max_queue')) is int and 0 <= audit['max_queue'] <= audit['capacity']),
            identity=(all(type(item.get(key)) is int and item[key] > 0
                for item in (producer, writer) for key in ('pid', 'thread_id', 'native_thread_id'))
                and producer['pid'] == writer['pid'] and producer['native_thread_id'] != writer['native_thread_id']
                and '..' not in parts and parts[-4:] == (raw.name, 'payload', 'logs', 'scenario_events.jsonl')))
        if not path.resolve().is_relative_to(raw.resolve()):
            raise ValueError('payload journal is outside its run')
        before = path.stat()
        digest, count, size, largest = hashlib.sha256(), 0, 0, 0
        with path.open('rb') as stream:
            while line := stream.readline(65537):
                if len(line) > 65536 or not line.endswith(b'\n'):
                    raise ValueError('payload journal record is oversized or incomplete')
                digest.update(line); count += 1; size += len(line); largest = max(largest, len(line))
        after = path.stat()
        checks['file'] = (checks['ledger'] and largest <= audit['max_record_bytes']
            and count == audit.get('written') and size == audit.get('written_bytes')
            and digest.hexdigest() == audit.get('sha256')
            and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and size == after.st_size)
        result.update(checks=checks, rows=count, bytes=size, sha256=digest.hexdigest(),
                      path='payload/logs/scenario_events.jsonl', audit_path='metrics/payload_channel.json')
        if all(checks.values()):
            result.update(status='verified_closed_payload_journal', passed=True)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result['error'] = str(error)
    return result


def coordination_journal_verification(raw):
    """Verify every configured endpoint's closed producer ledger and raw bytes."""
    raw = Path(raw)
    result = dict(status='invalid_coordination_journals', passed=False, journals={})
    try:
        def read(path):
            return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_json_fields,
                              parse_constant=reject_json_constant)
        environment = raw / 'environment.txt'
        text = environment.read_text(encoding='utf-8') if environment.is_file() else ''
        declared = re.findall(r'^coordination_journal_mode=(.*)$', text, re.M)
        paths = sorted((raw / 'logs').glob('coordination_*.jsonl'))
        audits = sorted((raw / 'logs').glob('coordination_*.jsonl.writer.json'))
        marked = bool(audits)
        for path in paths:
            with path.open('rb') as stream:
                first = stream.readline(65537)
            try:
                row = json.loads(first)
                marked = marked or (isinstance(row, dict) and 'journal_mode' in row)
            except (ValueError, UnicodeError):
                pass  # Legacy syntax remains the packet analyzer's responsibility.
        if not declared and not marked:
            return dict(status='not_requested_legacy', passed=None)
        if declared and declared != ['bounded_async_jsonl_v1']:
            raise ValueError('unsupported or duplicate advertised coordination journal mode')
        counts = re.findall(r'^uav_count=(.*)$', text, re.M)
        if len(counts) != 1 or counts[0] not in ('1', '2', '3', '4', '5'):
            raise ValueError('coordination journal requires one explicit UAV count in 1..5')
        count = int(counts[0])
        names = ['coordination_gcs.jsonl', *(f'coordination_uav{i}.jsonl' for i in range(1, count+1))]
        result['expected_node_ids'] = list(range(count+1))
        if {p.name for p in paths} != set(names) or {p.name for p in audits} != {n+'.writer.json' for n in names}:
            raise ValueError('configured coordination journals or writer audits are missing or unexpected')
        for node_id, name in enumerate(names):
            path = raw / 'logs' / name
            audit_path = path.with_name(name + '.writer.json')
            if any(not p.resolve().is_relative_to(raw.resolve()) for p in (path, audit_path)):
                raise ValueError('coordination journal or audit is outside its run')
            audit = read(audit_path)
            if not isinstance(audit, dict):
                raise ValueError('coordination writer audit must be an object')
            producer, writer = audit.get('producer'), audit.get('writer')
            if not isinstance(producer, dict) or not isinstance(writer, dict):
                raise ValueError('coordination writer process and thread identity is missing')
            parts = PurePosixPath(audit['path'].replace('\\', '/')).parts if isinstance(audit.get('path'), str) else ()
            checks = dict(
                metadata=(type(audit.get('schema')) is int and audit['schema'] == 1
                    and audit.get('journal_mode') == 'bounded_async_jsonl_v1'
                    and audit.get('journal_clock_domain') == 'host_monotonic'
                    and audit.get('run_id') == raw.name and type(audit.get('node_id')) is int
                    and audit['node_id'] == node_id and type(audit.get('uav_id')) is int
                    and audit['uav_id'] == node_id and audit.get('status') == 'closed'
                    and all(key in audit and audit[key] is None for key in ('producer_error', 'close_error'))),
                closed=(audit.get('closed') is True and audit.get('drained') is True and audit.get('listener_alive') is False
                    and all(type(audit.get(key)) is int and audit[key] == 0 for key in ('queue_depth', 'failed', 'rejected', 'error_count'))
                    and audit.get('first_exception') is None and audit.get('last_exception') is None
                    and audit.get('unwritten_records') == [] and audit.get('in_progress_record') is None
                    and audit.get('rejected_record') is None),
                ledger=(all(type(audit.get(key)) is int and audit[key] > 0
                    for key in ('accepted', 'written', 'accepted_bytes', 'written_bytes', 'capacity', 'max_record_bytes'))
                    and audit['accepted'] == audit['written'] and audit['accepted_bytes'] == audit['written_bytes']
                    and audit['capacity'] <= 512 and audit['max_record_bytes'] <= 65536
                    and type(audit.get('max_queue')) is int and 0 <= audit['max_queue'] <= audit['capacity']),
                identity=(all(type(item.get(key)) is int and item[key] > 0
                    for item in (producer, writer) for key in ('pid', 'thread_id', 'native_thread_id'))
                    and producer['pid'] == writer['pid'] and producer['native_thread_id'] != writer['native_thread_id']
                    and '..' not in parts and parts[-3:] == (raw.name, 'logs', name)))
            before = path.stat()
            digest, rows, size, largest, row_identity = hashlib.sha256(), 0, 0, 0, True
            with path.open('rb') as stream:
                while line := stream.readline(65537):
                    if len(line) > 65536 or not line.endswith(b'\n'):
                        raise ValueError('coordination journal record is oversized or incomplete')
                    row = json.loads(line, object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
                    row_identity = row_identity and (isinstance(row, dict)
                        and row.get('journal_mode') == 'bounded_async_jsonl_v1' and row.get('run_id') == raw.name
                        and type(row.get('node_id')) is int and row['node_id'] == node_id
                        and type(row.get('host_monotonic_ns')) is int and row['host_monotonic_ns'] > 0
                        and row.get('journal_clock_domain') == 'host_monotonic'
                        and type(row.get('journal_host_monotonic_ns')) is int
                        and row['journal_host_monotonic_ns'] > 0)
                    digest.update(line); rows += 1; size += len(line); largest = max(largest, len(line))
            after = path.stat()
            checks['file'] = (checks['ledger'] and row_identity and largest <= audit['max_record_bytes']
                and rows == audit['written'] and size == audit['written_bytes'] and digest.hexdigest() == audit.get('sha256')
                and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and size == after.st_size)
            result['journals'][str(node_id)] = dict(passed=all(checks.values()), checks=checks,
                rows=rows, bytes=size, sha256=digest.hexdigest(), path='logs/'+name,
                audit_path='logs/'+audit_path.name,
                audit_sha256=hashlib.sha256(audit_path.read_bytes()).hexdigest())
        if all(row['passed'] for row in result['journals'].values()):
            result.update(status='verified_closed_coordination_journals', passed=True)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result['error'] = str(error)
    return result


def native_library_verification(raw):
    from scripts.product.native_library_identity import verify
    return verify(raw)


def runtime_spool_verification(raw):
    from scripts.product.runtime_storage import verify
    return verify(raw)


def radio_bench_summary(raw, code, config, folder):
    """Qualify existing non-flight evidence, never physical hardware or flight success."""
    checks = dict(inputs=False, scenario=False, observation_window=False, native_radio=False,
                  readonly_exchange=False, external_endpoints=False, no_bypass=False)
    proof = dict(passed=False, checks=checks, scope='software radio-HitL boundary',
                 hardware_validation='not_performed', joint_flight_validation='not_performed')
    def read(path):
        return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_json_fields,
                          parse_constant=reject_json_constant)
    scenario = {}
    try:
        from scripts.product.test_radio_hitl import load_bundle
        profiles = folder / 'hitl-reference-profiles.json'
        record = read(folder / 'run.json')
        checks['inputs'] = all(hashlib.sha256((folder / name).read_bytes()).hexdigest() == record.get(key)
            for key, name in (('hitl_request_sha256', 'hitl-request.json'),
                              ('hitl_profiles_sha256', profiles.name), ('hitl_bundle_sha256', 'hitl-bundle.json')))
        count = config['drone']['count']
        ids = list(range(1, count + 1))
        expected = load_bundle(folder / 'hitl-bundle.json', profiles, native_uav_count=count)
        checks['inputs'] = checks['inputs'] and read(folder / 'hitl-request.json') == expected['source_request']
        actual_path = raw / 'external_endpoints/bundle.json'
        checks['inputs'] = checks['inputs'] and (raw / 'external_endpoints/reference-profiles.json').read_bytes() == profiles.read_bytes()
        watchdog = read(actual_path)['adapters']['control']['radio_watchdog_file']
        actual = load_bundle(actual_path, profiles, native_uav_count=count,
                             watchdog_file=watchdog, runtime_snapshot=True)
        for adapter in expected['adapters'].values():
            adapter['radio_watchdog_file'] = watchdog
        checks['inputs'] = checks['inputs'] and actual == expected
        scenario = read(raw / 'metrics/scenario_summary.json')
        if not isinstance(scenario, dict):
            raise ValueError('radio bench scenario summary must be an object')
        diagnostic = scenario.get('latency_diagnostic', {})
        checks['scenario'] = (scenario.get('status') == 'diagnostic_complete' and type(diagnostic.get('uav_count')) is int
            and scenario.get('mission_outcome') == 'not_applicable' and diagnostic.get('uav_count') == count
            and diagnostic.get('channels') == ['control', 'payload'])
        window = diagnostic.get('bench_window', {})
        start, end = window.get('started_host_monotonic_ns'), window.get('ended_host_monotonic_ns')
        counters = window.get('message_counts', {})
        checks['observation_window'] = (window.get('status') == 'completed' and window.get('clock') == 'host_monotonic'
            and window.get('requested_duration_s') == config['experiment']['duration_s']
            and type(start) is int and type(end) is int and start > 0
            and end - start >= round(config['experiment']['duration_s'] * 1e9)
            and set(counters) == {f'{channel}:uav{i}' for channel in ('control', 'payload') for i in ids}
            and all(type(n) is int and n >= 0 for n in counters.values()))
        stats = read(raw / 'metrics/native_radio_stats.json')
        checks['native_radio'] = (type(stats.get('uav_count')) is int and stats.get('uav_count') == count
            and type(stats.get('radio_node_count')) is int and stats.get('radio_node_count') == count + 1
            and stats.get('shared_spectrum_channel_count') == 1 and stats.get('native_ns3_phy') is True
            and stats.get('native_ns3_mac') is True and stats.get('packet_outcome_affected') is True
            and stats.get('selected_propagation_profile') == config['propagation']['model'])
        latency = read(raw / 'metrics/control_latency_summary.json')
        payload = diagnostic.get('payload_version_check', {})
        operations = scenario.get('command_operations', [])
        checks['readonly_exchange'] = (latency.get('status') == 'diagnostic_complete'
            and latency.get('uav_count') == count
            and latency.get('mavlink_latency') == read(raw / 'metrics/mavlink_latency_summary.json')
            and payload.get('status') == 'passed' and set(payload.get('uavs', {})) == {str(i) for i in ids}
            and all(payload['uavs'][str(i)].get('status') == 'passed'
                    and payload['uavs'][str(i)].get('accepted_ack_received') is True
                    and payload['uavs'][str(i)].get('response_received') is True for i in ids)
            and all(any(op.get('uav') == f'uav{i}' and op.get('channel') == 'control' and op.get('command') == 512
                        and any(a.get('outcome') == 'ack_received' and type(a.get('send_monotonic_ns')) is int
                                and type(a.get('ack_gcs_received_monotonic_ns')) is int
                                and a['ack_gcs_received_monotonic_ns'] >= a['send_monotonic_ns']
                                and a.get('command_frame_hex') and a.get('ack_frame_hex')
                                for a in op.get('attempts', [])) for op in operations) for i in ids))
        pair = raw / 'external_endpoints'
        pair_result, ready = read(pair / 'result.json'), read(pair / 'ready.json')
        endpoint_checks = []
        identities = pair_result.get('serial_device_identities', {})
        serial_channels = {c for c in ('control', 'payload') if expected['adapters'][c]['endpoint']['kind'] == 'serial'}
        serial_identity_valid = (set(identities) == serial_channels
            and all(identities[c].get('configured_path') == expected['adapters'][c]['endpoint']['device']
                    and type(identities[c].get('st_rdev')) is int for c in serial_channels)
            and len({identities[c]['st_rdev'] for c in serial_channels}) == len(serial_channels))
        for channel in ('control', 'payload'):
            metrics = read(pair / channel / 'metrics.json')
            endpoint_checks.append(metrics.get('uav_id') == expected['native_binding']['uav_id']
                and metrics.get('channel') == channel
                and metrics.get('controller_kind') == expected['adapters'][channel]['endpoint']['kind']
                and all(type(metrics.get(k)) is int and metrics[k] > 0
                        for k in ('endpoint_to_radio_bytes', 'radio_to_endpoint_bytes'))
                and type(metrics.get('io_errors')) is int and metrics['io_errors'] == 0
                and type(metrics.get('reconnects')) is int and metrics['reconnects'] == 1
                and (channel not in serial_channels or (serial_identity_valid and metrics.get('serial_exclusive') is True
                    and type(metrics.get('serial_device_identity')) is int
                    and metrics['serial_device_identity'] == identities[channel]['st_rdev']))
                and not (raw / f'metrics/{channel}_uart_uav{expected["native_binding"]["uav_id"]}.json').exists())
        codes = pair_result.get('child_returncodes', {})
        checks['external_endpoints'] = (ready.get('adapter_endpoints_connected') is True
            and pair_result.get('status') in ('completed', 'stopped') and not pair_result.get('error')
            and set(codes) == {'control', 'payload'} and all(type(n) is int and n == 0 for n in codes.values())
            and serial_identity_valid and all(endpoint_checks))
        isolation = read(raw / 'metrics/no_bypass_summary.json')
        slot_ids = {f'{channel}:uav{i}' for channel in ('control', 'payload') for i in ids}
        checks['no_bypass'] = (isolation.get('passed') is True and isolation.get('uav_ids') == ids
            and isolation.get('channels') == ['control', 'payload']
            and all(isolation.get(k) is True for k in ('uart_metrics_complete', 'transport_sequences_resumed',
                'physics_progressed_all_uavs', 'control_ack_absent', 'payload_response_absent', 'reverse_telemetry_absent'))
            and set(isolation.get('uart_delivered_bytes_delta', {})) == slot_ids
            and all(type(n) is int and n == 0 for n in isolation['uart_delivered_bytes_delta'].values())
            and set(isolation.get('source_uart_bytes_delta', {})) == slot_ids
            and all(type(n) is int and n > 0 for n in isolation['source_uart_bytes_delta'].values()))
        proof['passed'] = code == 0 and all(checks.values())
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        proof['error'] = str(error)
    scenario = scenario if isinstance(scenario, dict) else {}
    stopped = code == 130 and scenario.get('status') == 'stopped'
    return dict(status='completed' if proof['passed'] else 'stopped' if stopped else 'failed',
        runtime_status='completed' if proof['passed'] or stopped else 'failed', mission_outcome='not_applicable',
        measurement_validity='valid' if proof['passed'] else 'not_evaluated' if stopped else 'invalid',
        error=None if proof['passed'] else scenario.get('error') or 'radio bench evidence is incomplete or inconsistent',
        radio_hitl_verification=proof)


def recorded_outcome(raw, code, summary, *, source_configured=False):
    """Use backend evidence as well as mission status; never infer success from exit 0."""
    result = {key: summary.get(key, 'not_evaluated') for key in
              ('runtime_status', 'mission_outcome', 'measurement_validity')}
    if 'radio_hitl_verification' in summary:
        result['radio_hitl_verification'] = summary['radio_hitl_verification']
        result['hardware_validation'] = 'not_performed'
    stats_path = raw / 'metrics/native_radio_stats.json'
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    verification_errors = []
    result['native_stop_reason'] = stats.get('stop_reason', 'not_recorded')
    if stats.get('stop_reason') not in ('duration', 'sionna_ns3_process_stopped'):
        result['runtime_status'] = 'failed'
        verification_errors.append('native_stop_not_qualified')
        if result['measurement_validity'] == 'valid':
            result['measurement_validity'] = 'invalid'
        if stats.get('stop_reason') in ('position_tracker_stale', 'state_coupling_deadline', 'runtime_error', 'sionna_python_error'):
            result['measurement_validity'] = 'invalid'
    if code != 0 and not summary.get('error'):
        result['runtime_status'] = 'failed'
        verification_errors.append('runner_exit_not_explained_by_mission')
    storage_path = raw / 'logs/native_capture_storage.json'
    storage = json.loads(storage_path.read_text()) if storage_path.exists() else {}
    publication = storage.get('publication', {})
    if publication.get('status') not in ('copied', 'direct_disk') or publication.get('errors'):
        verification_errors.append('native_capture_publication_not_confirmed')
    if not (raw / 'configuration/final/manifest.json').is_file():
        verification_errors.append('final_configuration_capture_missing')
    result['node_state_journal_verification'] = node_state_journal_verification(raw)
    if result['node_state_journal_verification']['passed'] is False:
        verification_errors.append('node_state_journal_invalid')
        result['measurement_validity'] = 'invalid'
    result['payload_journal_verification'] = payload_journal_verification(raw)
    if result['payload_journal_verification']['passed'] is False:
        verification_errors.append('payload_journal_invalid')
        result['measurement_validity'] = 'invalid'
    result['coordination_journal_verification'] = coordination_journal_verification(raw)
    if result['coordination_journal_verification']['passed'] is False:
        verification_errors.append('coordination_journal_invalid')
        result['measurement_validity'] = 'invalid'
    result['native_library_verification'] = native_library_verification(raw)
    if result['native_library_verification']['passed'] is False:
        verification_errors.append('native_library_identity_invalid')
        result['measurement_validity'] = 'invalid'
    result['runtime_spool_verification'] = runtime_spool_verification(raw)
    if result['runtime_spool_verification']['passed'] is False:
        verification_errors.append('runtime_spool_publication_invalid')
        result['measurement_validity'] = 'invalid'
    if (not verification_errors and result['runtime_status'] == 'completed'
            and result['measurement_validity'] == 'valid'):
        source_input = raw / 'logs/native_sources.json'
        source_summary = raw / 'metrics/native_source_summary.json'
        environment_path = raw / 'environment.txt'
        environment_text = environment_path.read_text(encoding='utf-8', errors='replace') if environment_path.is_file() else ''
        pool_required = (stats.get('sionna_rt_execution_mode') == 'worker_pool'
                         or re.search(r'^sionna_rt_execution_mode=worker_pool\s*$', environment_text, re.M) is not None
                         or (raw / 'logs/native_rt_worker_config.json').exists())
        if pool_required:
            try:
                from scripts.product.summarize_native_radio_five_uav import load_scenario_config, native_rt_worker_contract
                scenario = load_scenario_config(control_dir(raw.name) / 'generated/scenario.yaml')
                proof = native_rt_worker_contract(stats, scenario, run_dir=raw)
                result['native_rt_worker_verification'] = proof
                frozen = json.loads((raw / 'logs/native_rt_workers/summary.json').read_text(encoding='utf-8'),
                                    object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
                if proof.get('passed') is not True or frozen != stats.get('sionna_rt_workers'):
                    raise ValueError('native RT worker evidence is incomplete or disagrees with the closed summary')
            except (OSError, ValueError, TypeError, KeyError):
                verification_errors.append('native_rt_worker_export_invalid')
        declared = re.search(r'^native_sources=(.+)$', environment_text, re.M)
        source_required = source_configured or source_input.exists() or (
            declared is not None and declared.group(1).strip() not in ('', 'none'))
        if source_required:
            if not source_input.is_file():
                verification_errors.append('native_source_input_missing')
            elif not source_summary.is_file():
                verification_errors.append('native_source_summary_missing')
            else:
                try:
                    # Validate existing evidence only: never create an export or
                    # infer source activity/effect from its configured interval.
                    from scripts.product.summarize_native_radio_five_uav import _native_source_schedule, _native_source_capture
                    expected = _native_source_schedule(raw)
                    capture = _native_source_capture(raw)
                    observed = json.loads(source_summary.read_text(encoding='utf-8'),
                        object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
                    if (expected['actual_schedule_status'] != 'observed' or capture['status'] != 'observed'
                            or not isinstance(observed, dict) or observed.get('event_capture') != capture
                            or any(observed.get(key) != expected[key] for key in expected)):
                        raise ValueError('source export does not match the validated native schedule')
                except (OSError, ValueError, TypeError, KeyError):
                    verification_errors.append('native_source_export_invalid')
    if verification_errors:
        result['runtime_status'] = 'failed'
        if result['measurement_validity'] == 'valid':
            result['measurement_validity'] = 'invalid'
    result['runtime_verification_errors'] = verification_errors
    return result


def request_stop(run_id):
    folder = control_dir(run_id)
    state = json.loads((folder / 'run.json').read_text())
    if state['status'] != 'running':
        raise ValueError(f'run is not active: {state["status"]}')
    (folder / 'stop.request').touch()
    if state.get('environment', {}).get('BAS_NATIVE_EXTERNAL_BUNDLE'):
        print('Radio bench stop requested; external controller is not commanded to fly or LAND.')
    else:
        print('Stop requested; the mission will attempt LAND through ns-3. Check status for the outcome.')


def run(config, run_id):
    validate_capture_storage(config)
    if sys.platform != 'linux':
        raise ValueError('simulation runtime requires Linux; inspect, status and events are available on this host')
    import fcntl  # Host preparation and observation do not require the Linux runtime.
    folder, raw = control_dir(run_id), raw_dir(run_id)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another BAS runtime owns the host lock.', file=sys.stderr)
            return 75
        if folder.exists() or folder.is_symlink() or raw.exists() or raw.is_symlink():
            raise ValueError('run already exists; choose a new ID')
        for path in ['install/setup.bash', '.external/ns-3-sionna-native/build/scratch/ns3.48-upstream-sionna-tap-spike-default']:
            if not (ROOT / path).is_file():
                raise ValueError(f'missing runtime dependency: {path}; see README')
        subprocess.run(['docker', 'image', 'inspect', config['runtime']['image']],
                       check=True, stdout=subprocess.DEVNULL)
        folder.mkdir(parents=True)
        env = environment(config, run_id)
        spool = None
        runtime_log = folder / 'runtime.log'
        if config['runtime'].get('capture_storage', 'tmpfs') == 'tmpfs':
            from scripts.product import runtime_storage
            spool = runtime_storage.prepare(ROOT, run_id, env['BAS_NATIVE_FIVE_CONTAINER_NAME'], image=config['runtime']['image'])
            env['BAS_RUNTIME_SPOOL'] = spool['spool']
            runtime_log = Path(spool['runtime_log_source'])
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        hashes = snapshot_inputs(config, folder, env)
        argv = ['./network/ns3/run_native_radio_five_uav.sh']
        record = dict(run_id=run_id, status='running', argv=argv,
                      source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                      source_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                      **hashes,
                      environment={k: v for k, v in env.items() if k.startswith('BAS_')},
                      started_host_monotonic_ns=time.monotonic_ns(),
                      started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
        write_json(folder / 'run.json', record)
        previous = {}
        def on_stop(signum, frame):
            (folder / 'stop.request').touch()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, on_stop)
        print(f'Run {run_id}: {folder / "runtime.log"}', flush=True)
        try:
            with runtime_log.open('x') as log:
                child = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True, pass_fds=(lock.fileno(),))
                code = child.wait()
            record['controller_returncode'] = code
            summary_path = raw / 'metrics/relay_experiment_summary.json'
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            if config.get('hitl') is not None:
                summary = radio_bench_summary(raw, code, config, folder)
            # The bench summary is the last parent raw writer. Publication must
            # follow it as well as child/container exit and runtime-log closure.
            if spool is not None:
                try:
                    runtime_storage.publish(ROOT, spool, code)
                except Exception as error:
                    record['runtime_spool_publication_error'] = str(error)
                    code = code or 1
            status = summary.get('status', 'failed') if code == 0 or (
                config.get('hitl') is not None and code == 130 and summary.get('status') == 'stopped') else 'failed'
            record.update(status=status, returncode=code, mission_error=summary.get('error'))
            record.update(recorded_outcome(raw, record['controller_returncode'], summary,
                source_configured=config.get('interference', {}).get('sources') is not None))
        except Exception as error:
            record.update(status='failed', error=str(error))
            raise
        finally:
            record['ended_host_monotonic_ns'] = time.monotonic_ns()
            write_json(folder / 'run.json', record)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        print(json.dumps({key: record[key] for key in
                         ('run_id', 'runtime_status', 'mission_outcome', 'measurement_validity', 'returncode')}))
        return code if code else (130 if record['status'] == 'stopped' else
                                 0 if record['runtime_status'] == 'completed'
                                      and (record['mission_outcome'] == 'succeeded' or (
                                          config.get('hitl') is not None and record['mission_outcome'] == 'not_applicable'
                                          and record.get('radio_hitl_verification', {}).get('passed') is True))
                                      and record['measurement_validity'] == 'valid' else 1)


def events(paths, command_id=None, event=None, limit=50):
    latest, serial = [], 0
    for path in paths:
        with path.open() as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.endswith('\n'):
                    print(f'{path.name}:{line_number}: incomplete final record skipped', file=sys.stderr)
                    continue
                row = json.loads(line)  # A corrupt complete record must not disappear silently.
                if command_id is not None and row.get('command_id') != command_id:
                    continue
                if event is not None and row.get('event') != event:
                    continue
                if command_id is None and event is None and row.get('event') not in MILESTONES:
                    continue
                stamp = row['host_monotonic_ns']
                row['log_file'] = path.name
                serial += 1
                heapq.heappush(latest, (stamp, serial, row))
                if len(latest) > limit:
                    heapq.heappop(latest)
    return [row for _, _, row in sorted(latest)]


def analyze(run_id, output, *, service_batch=False):
    """Analyze closed raw evidence inside its existing pinned runtime image."""
    from scripts.product import bootstrap_native_runtime as native
    if sys.platform != 'linux':
        raise ValueError('container analysis requires a Linux Docker host')
    run_id = identifier(run_id)
    folder, raw = control_dir(run_id), raw_dir(run_id)
    record_path, config_path = folder / ('batch.json' if service_batch else 'run.json'), folder / 'config.json'
    record = json.loads(record_path.read_text(encoding='utf-8'),
                        object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
    start, end = record.get('started_host_monotonic_ns'), record.get('ended_host_monotonic_ns')
    terminal = ('completed', 'completed_with_failures', 'failed', 'cancelled', 'stopped_on_failure') if service_batch else ('completed', 'failed', 'stopped')
    identity_field = 'batch_id' if service_batch else 'run_id'
    if (record.get(identity_field) != run_id or record.get('status') not in terminal
            or (not service_batch and record.get('runtime_status') not in terminal)
            or type(start) is not int or type(end) is not int or not 0 <= start < end
            or type(record.get('returncode')) is not int):
        raise ValueError('analysis requires a closed terminal run record, including failed attempts')
    if not service_batch and not raw.is_dir():
        raise ValueError('closed run has no preserved raw directory')
    config = record.get('config', {}) if service_batch else json.loads(
        config_path.read_text(encoding='utf-8'),
        object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
    bindings = (record_path,) if service_batch else (record_path, config_path)
    image = config.get('runtime', {}).get('image')
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('closed run requires its recorded immutable runtime image')
    output = (ROOT / output).resolve()
    output.relative_to(ROOT.resolve())
    if any(output.is_relative_to(protected.resolve()) for protected in (raw, folder)):
        raise ValueError('analysis output must not overlap raw or control evidence')
    output.relative_to((ROOT / 'runs/analysis').resolve())
    if output == (ROOT / 'runs/analysis').resolve() or output.exists():
        raise ValueError('analysis requires a new directory below runs/analysis')
    output.mkdir(parents=True, exist_ok=False)
    attempt = dict(schema_version=1, run_id=run_id, status='running', image=image,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
        analysis_mode='service_batch' if service_batch else 'single_run',
        analyzed_batch_status=record['status'] if service_batch else None,
        analyzed_runtime_status=record.get('runtime_status'),
        analyzed_measurement_validity=record.get('measurement_validity'),
        analyzer_sha256=native.sha256(ROOT / 'scripts/product/analyze_relay_experiment.py'),
        adapter_sha256=native.sha256(Path(__file__)),
        container_helper_sha256=native.sha256(Path(native.__file__)),
        source_bindings=[dict(path=path.relative_to(ROOT).as_posix(), sha256=native.sha256(path),
                              bytes=path.stat().st_size) for path in bindings],
        started_host_monotonic_ns=time.monotonic_ns(),
        scope='Existing CPU analyzer; full original captures read-only; no new simulation, GPU, or host pymavlink.',
        options=dict(plots=False,live_export=False),
        container_permissions=dict(user=f'{os.getuid()}:{os.getgid()}', supplementary_groups=[1000],
            basis='Pinned image ubuntu home0750/group1000 contains existing pymavlink; output retains caller UID.'))
    receipt = output / 'analysis-attempt.json'
    write_json(receipt, attempt)
    # Run IDs have already passed identifier(); script contains no user shell text.
    analyzer_arguments = (
        f'--service-batch {native.MOUNT}/runs/control/{run_id}/batch.json '
        f'--runs-root {native.MOUNT}/runs/native-radio-realtime --output-dir /bas-analysis-output'
        if service_batch else
        f'--run-dir {native.MOUNT}/runs/native-radio-realtime/{run_id} '
        '--output-dir /bas-analysis-output --no-plots --skip-live-export')
    script = (
        'export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1\n'
        'export MPLCONFIGDIR=/tmp/bas-analysis-mpl\n'
        'SDK="$PWD/.external/ns-3-sionna-native"\n'
        'export PYTHONPATH="$SDK/.tooling-py310:$SDK/.python-deps-py310:/home/ubuntu/.local/lib/python3.10/site-packages:$PWD"\n'
        'python3 - <<\'PY\'\n'
        'import sys,numpy,yaml,pymavlink\n'
        'assert sys.version_info[:2] == (3,10), "pinned analysis image requires Python3.10"\n'
        'assert (numpy.__version__,yaml.__version__,pymavlink.__version__) == '
        '("1.26.4","6.0.2","2.4.49"), "analysis dependency versions differ"\n'
        'PY\n'
        'python3 scripts/product/analyze_relay_experiment.py '
        + analyzer_arguments + '\n')
    try:
        native.image_id(image)
        native.container(ROOT, image, script, readonly=True, output=output,
                         supplementary_groups=(1000,), log=output / 'analysis-runtime.log')
        for item in attempt['source_bindings']:
            if native.sha256(ROOT / item['path']) != item['sha256']:
                raise ValueError('closed run metadata changed during analysis')
        report = 'service_series_analysis.json' if service_batch else 'relay_analysis.json'
        if not (output / 'metrics' / report).is_file():
            raise ValueError(f'analyzer did not produce {report}')
        attempt['status'] = 'completed'
    except BaseException as error:
        attempt.update(status='cancelled' if isinstance(error, KeyboardInterrupt) else 'failed',
                       error=f'{type(error).__name__}: {error}')
        raise
    finally:
        attempt['ended_host_monotonic_ns'] = time.monotonic_ns()
        write_json(receipt, attempt)
    print(json.dumps(dict(identity=run_id, analysis_status=attempt['status'],
                          batch_status=record['status'] if service_batch else None,
                          runtime_status=record.get('runtime_status'),
                          measurement_validity=record.get('measurement_validity'),
                          output=output.relative_to(ROOT).as_posix())))
    return 0


def maps(config, output, *, service_map_from=None, service_evidence=None,
         service_profile=None, validate_only=False, plot=False,
         variant='cuda_ad_mono_polarized'):
    """Run the existing map tool with one owned writable directory."""
    from contextlib import nullcontext
    from scripts.product import bootstrap_native_runtime as native
    os.environ['OPENBLAS_NUM_THREADS'] = '1'
    from scripts.product import compute_radio_maps as mapper
    if sys.platform != 'linux':
        raise ValueError('container maps require a Linux Docker host')
    if variant not in ('cuda_ad_mono_polarized', 'llvm_ad_mono_polarized'):
        raise ValueError('unsupported existing map runtime variant')
    if validate_only and plot:
        raise ValueError('--plot excludes --validate-only')
    overlay = service_map_from is not None
    if (overlay and (config is not None or validate_only or service_evidence is None)
            or not overlay and (config is None or service_evidence is not None or service_profile is not None)):
        raise ValueError('choose RF --config or CPU --service-map-from with --service-evidence')
    def inside(value, *, directory=False):
        path = (ROOT / value).resolve()
        path.relative_to(ROOT.resolve())
        if not (path.is_dir() if directory else path.is_file()):
            raise ValueError(f'map input is missing: {value}')
        return path
    manifest_path = inside('.devcontainer/runtime-assets.json')
    manifest = mapper.load_json(manifest_path)
    image = manifest['image_id']
    bindings = [manifest_path]
    for relative in (native.RECEIPT, '.external/native-runtime-wheels/manifest.json'):
        candidate = ROOT / relative
        if candidate.is_file():
            bindings.append(inside(candidate))
    arguments = []
    if overlay:
        snapshot = inside(service_map_from, directory=True)
        evidence = inside(service_evidence)
        profile = inside(service_profile or 'network/config/service_profiles_v1.json')
        metadata = inside(snapshot / 'metadata.json')
        if mapper.load_json(metadata).get('status') != 'completed':
            raise ValueError('maps overlay requires a closed completed RF snapshot')
        bindings += [metadata, inside(snapshot / 'maps.npz'), evidence, profile]
        for option, path in (('--service-map-from', snapshot), ('--service-evidence', evidence),
                             ('--service-profile', profile)):
            arguments += [option, str(Path(native.MOUNT) / path.relative_to(ROOT))]
    else:
        config = inside(config)
        paths, mission, _, _ = mapper.validate(mapper.load_json(config), root=ROOT)
        bindings += [config, *paths.values(), inside(paths['scene_xml'].parent / 'scene-manifest.json')]
        if mission.terrain_manifest is not None:
            bindings += [inside(mission.terrain_bundle_path / name) for name in mission.terrain_manifest['files']]
        for element in ET.parse(paths['scene_xml']).findall('.//string[@name="filename"]'):
            bindings.append(inside(paths['scene_xml'].parent / element.get('value')))
        arguments += ['--config', str(Path(native.MOUNT) / config.relative_to(ROOT)), '--variant', variant]
    maps_root = ROOT / 'runs/maps'
    if maps_root.resolve() != maps_root:
        raise ValueError('maps require the canonical runs/maps directory without symlink redirection')
    output = (ROOT / output).resolve()
    output.relative_to(maps_root)
    if output == (ROOT / 'runs/maps').resolve() or output.exists():
        raise ValueError('maps require a new directory below runs/maps')
    if overlay and output.is_relative_to(snapshot):
        raise ValueError('map output must not overlap the input snapshot')
    destination = Path(native.MOUNT) / output.relative_to(ROOT)
    arguments += ['--output', str(destination / 'data')]
    if validate_only:
        arguments += ['--validate-only']
    if plot:
        arguments += ['--plot']
    gpu = not overlay and not validate_only and variant.startswith('cuda_')
    # Same non-blocking lease as simulation/bootstrap, before creating an attempt.
    with (native.runtime_lock() if not overlay and not validate_only else nullcontext()):
        input_bindings = [dict(path=path.relative_to(ROOT).as_posix(), bytes=path.stat().st_size,
                               sha256=native.sha256(path)) for path in sorted(set(bindings))]
        native.image_id(image)
        output.mkdir(parents=True, exist_ok=False)
        attempt = dict(schema_version=1, status='running', mode='service_overlay' if overlay else 'rf',
            validation_only=validate_only, gpu_requested=gpu, image=image, variant=None if overlay else variant,
            source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            source_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
            adapter_sha256=native.sha256(Path(__file__)),
            mapper_sha256=native.sha256(ROOT / 'scripts/product/compute_radio_maps.py'),
            container_helper_sha256=native.sha256(Path(native.__file__)),
            source_bindings=input_bindings, started_host_monotonic_ns=time.monotonic_ns(),
            container_permissions=dict(user=f'{os.getuid()}:{os.getgid()}', supplementary_groups=[1000],
                source='read_only', output='single_owned_workspace_directory', network='none'),
            scope='Existing RF solver or transport overlay; no new channel model, packet simulation, or hardware claim.',
            dependency_scope='Available pinned-image/SDK package versions; service overlays and validation do not execute RT packages. RF computation independently enforces the existing solver versions.')
        receipt = output / 'maps-attempt.json'
        write_json(receipt, attempt)
        dependency_path = destination / 'maps-dependencies.json'
        program = (
            'import importlib.metadata,json,pathlib,runpy,sys\n'
            'import numpy,yaml\n'
            'assert sys.version_info[:2] == (3,10), "pinned map image requires Python3.10"\n'
            'assert (numpy.__version__,yaml.__version__) == ("1.26.4","6.0.2"), "map dependency versions differ"\n'
            'versions={}\n'
            'for name in ("numpy","PyYAML","pymavlink","matplotlib","sionna-rt","mitsuba","drjit"):\n'
            ' try: versions[name]=importlib.metadata.version(name)\n'
            ' except importlib.metadata.PackageNotFoundError: versions[name]=None\n'
            + f'pathlib.Path({str(dependency_path)!r}).write_text(json.dumps(versions,indent=2)+"\\n")\n'
            + f'sys.argv=["compute_radio_maps.py"]+{arguments!r}\n'
            'runpy.run_path("scripts/product/compute_radio_maps.py",run_name="__main__")\n')
        script = (
            'export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1\n'
            'export MPLCONFIGDIR=/tmp/bas-maps-mpl\n'
            'SDK="$PWD/.external/ns-3-sionna-native"\n'
            'export PYTHONPATH="$SDK/.tooling-py310:$SDK/.python-deps-py310:/home/ubuntu/.local/lib/python3.10/site-packages:$PWD"\n'
            "python3 - <<'BAS_MAPS_PY'\n" + program + 'BAS_MAPS_PY\n')
        try:
            native.container(ROOT, image, script, readonly=True, output=output,
                workspace_output=True, gpu=gpu, supplementary_groups=(1000,), log=output / 'maps-runtime.log')
            for item in input_bindings:
                if native.sha256(ROOT / item['path']) != item['sha256']:
                    raise ValueError('map input changed during computation')
            if not validate_only:
                name = 'service_metadata.json' if overlay else 'metadata.json'
                if mapper.load_json(output / 'data' / name).get('status') != 'completed':
                    raise ValueError('existing map tool did not report completed output')
            attempt['dependencies'] = mapper.load_json(output / 'maps-dependencies.json')
            attempt['output_files'] = [dict(path=path.relative_to(output).as_posix(),
                bytes=path.stat().st_size, sha256=native.sha256(path))
                for path in sorted((output / 'data').rglob('*')) if path.is_file()]
            attempt['status'] = 'completed'
        except BaseException as error:
            attempt.update(status='cancelled' if isinstance(error, KeyboardInterrupt) else 'failed',
                       error=f'{type(error).__name__}: {error}')
            raise
        finally:
            attempt['ended_host_monotonic_ns'] = time.monotonic_ns()
            write_json(receipt, attempt)
    print(json.dumps(dict(status=attempt['status'], validation_only=validate_only,
        output=output.relative_to(ROOT).as_posix(), data=None if validate_only else (output / 'data').relative_to(ROOT).as_posix())))
    return 0


def main():
    from scripts.product.town01_runtime_monitor import limit_host_math_threads
    limit_host_math_threads()
    if len(sys.argv) > 1 and sys.argv[1] == 'ui':
        from scripts.product.operator_ui import main as ui_main
        return ui_main(['--serve', *sys.argv[2:]])
    if len(sys.argv) > 1 and sys.argv[1] == 'batch':
        from scripts.product.run_batch import main as batch_main
        return batch_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('batch', help='sequential parameter/seed runs; use batch --help')
    commands.add_parser('ui', help='local operator interface on 127.0.0.1; use ui --help')
    analysis = commands.add_parser('analyze', help='CPU analysis of a closed run inside its pinned image')
    analysis.add_argument('run_id', help='Closed run ID, or batch ID with --service-batch')
    analysis.add_argument('--service-batch', action='store_true', help='Compare all attempts in a closed batch using original packet lineage')
    analysis.add_argument('--output-dir', required=True, type=Path, help='New directory below runs/analysis; raw evidence remains read-only')
    map_command = commands.add_parser('maps', help='Existing RF snapshots or observed service overlays in the pinned image')
    map_command.add_argument('--config', type=Path)
    map_command.add_argument('--output-dir', required=True, type=Path)
    map_command.add_argument('--service-map-from', type=Path)
    map_command.add_argument('--service-evidence', type=Path)
    map_command.add_argument('--service-profile', type=Path)
    map_command.add_argument('--validate-only', action='store_true')
    map_command.add_argument('--plot', action='store_true')
    map_command.add_argument('--variant', choices=('cuda_ad_mono_polarized','llvm_ad_mono_polarized'), default='cuda_ad_mono_polarized')
    for name in ('inspect', 'run'):
        p = commands.add_parser(name)
        p.add_argument('--config', type=Path, default=ROOT / 'runtime.json')
        p.add_argument('--run-id', default='core-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        p.add_argument('--duration', type=float)
    for name in ('status', 'stop', 'events'):
        p = commands.add_parser(name)
        p.add_argument('run_id')
        if name == 'events':
            p.add_argument('--command-id')
            p.add_argument('--event')
            p.add_argument('--limit', type=int, default=50)
    args = parser.parse_args()
    if args.command != 'maps':
        identifier(args.run_id)
    if args.command in ('inspect', 'run'):
        config = load_config(args.config)
        if args.duration is not None:
            config['experiment']['duration_s'] = validate_duration(args.duration)
            validate_capture_storage(config)
        if args.command == 'run':
            return run(config, args.run_id)
        print(json.dumps({'config': config, 'environment': {k: v for k, v in environment(config, args.run_id).items()
                                                          if k.startswith('BAS_')}}, indent=2))
    elif args.command == 'maps':
        return maps(args.config, args.output_dir, service_map_from=args.service_map_from,
                    service_evidence=args.service_evidence, service_profile=args.service_profile,
                    validate_only=args.validate_only, plot=args.plot, variant=args.variant)
    elif args.command == 'analyze':
        return analyze(args.run_id, args.output_dir, service_batch=args.service_batch)
    elif args.command == 'stop':
        request_stop(args.run_id)
    elif args.command == 'status':
        record = json.loads((control_dir(args.run_id) / 'run.json').read_text())
        phase = raw_dir(args.run_id) / 'logs/current_phase.txt'
        record['phase'] = phase.read_text().strip() if phase.exists() else None
        summary = raw_dir(args.run_id) / 'metrics/relay_experiment_summary.json'
        record['mission'] = json.loads(summary.read_text()) if summary.exists() else None
        print(json.dumps(record, indent=2))
    else:
        if not 1 <= args.limit <= 10000:
            raise ValueError('limit must be between 1 and 10000')
        paths = sorted((raw_dir(args.run_id) / 'logs').glob('coordination_*.jsonl'))
        if not paths:
            raise ValueError('no coordination journals for this run')
        for row in events(paths, args.command_id, args.event, args.limit):
            print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt as exc:
        signum = exc.args[0] if exc.args and exc.args[0] in (signal.SIGINT, signal.SIGTERM) else signal.SIGINT
        print(f'BAS: cancelled by signal {int(signum)}', file=sys.stderr)
        sys.exit(128 + int(signum))
    except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f'BAS: {exc}', file=sys.stderr)
        sys.exit(2)
