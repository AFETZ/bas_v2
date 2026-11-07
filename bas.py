#!/usr/bin/env python3
"""One-host BAS runtime: explicit configuration, owned lifecycle, raw event views."""
import argparse
import datetime
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
LOCK = Path('/tmp/bas-paper-runtime.lock')
SHAPE = {
    'environment': {'engine', 'scene'}, 'drone': {'autopilot', 'count'},
    'propagation': {'model'}, 'network': {'engine', 'topology'},
    'experiment': {'policy', 'duration_s', 'seed', 'run'},
    'runtime': {'clock', 'image', 'land_timeout_s', 'sionna_log_level', 'event_logging', 'capture_storage'},
    'flows': {'channels', 'payload_attitude_interval_s'},
    'interference': {'sources'},
}
PROPAGATION_FIELDS = {
    'geometry_logdistance': {'model'},
    'sionna': {'model', 'radio_config', 'ground_material', 'building_material', 'thickness_m'},
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


def runtime_duration_limits(duration, land_timeout_s, uav_count, channels, *, mode='relay_experiment', parameter_snapshots=False):
    """Lifetimes only: existing readiness/flight/stop deadlines remain unchanged."""
    validate_duration(duration)
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


def _interference_input(config, root):
    """Validate exact source bytes with the existing native schedule producer."""
    name = config.get('interference', {}).get('sources')
    if name is None:
        return None, None
    if config['propagation']['model'] != 'sionna':
        raise ValueError('interference.sources requires propagation.model=sionna')
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
    return path, raw


def load_config(path, root=ROOT):
    config = json.loads(path.read_text(encoding='utf-8-sig'),
                        object_pairs_hook=unique_json_fields, parse_constant=reject_json_constant)
    if isinstance(config, dict):
        config.setdefault('interference', {'sources': None})
    if not isinstance(config, dict) or set(config) != set(SHAPE):
        raise ValueError('runtime config has missing or unknown sections')
    if isinstance(config['runtime'], dict):
        config['runtime'].setdefault('sionna_log_level', 'info')
        config['runtime'].setdefault('event_logging', 'metrics_only')
        config['runtime'].setdefault('capture_storage', 'tmpfs')
    for section, keys in SHAPE.items():
        if section == 'propagation' and isinstance(config[section], dict):
            model = config[section].get('model')
            if not isinstance(model, str) or model not in PROPAGATION_FIELDS:
                raise ValueError('propagation.model must be geometry_logdistance or sionna; hybrid is not qualified')
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
    if type(count) is not int or not 1 <= count <= 5:
        raise ValueError('drone.count must be an integer from 1 to 5')
    channels = config['flows']['channels']
    if channels not in (['control'], ['control', 'payload']):
        raise ValueError('flows.channels must be [control] or [control, payload] in that order')
    interval = config['flows']['payload_attitude_interval_s']
    try:
        valid_interval = type(interval) in (int, float) and math.isfinite(interval) and .02 <= interval <= 60
    except OverflowError:
        valid_interval = False
    if not valid_interval:
        raise ValueError('flows.payload_attitude_interval_s must be finite and between .02 and 60 seconds')
    if config['experiment']['policy'] not in ('fixed', 'failover'):
        raise ValueError('policy must be fixed or failover')
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
    if tuple(sorted(scenario.spawns)) != tuple(range(1, count + 1)):
        raise ValueError('scene UAV roster must match drone.count exactly')
    if scenario.terrain_bundle_path is not None and config['propagation']['model'] != 'sionna':
        raise ValueError('terrain_bundle requires Sionna; flat geometry propagation cannot represent terrain')
    if config['propagation']['model'] == 'sionna':
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
        if radio_profile['sionna']['execution_mode'] == 'worker_pool' and config.get('interference'):
            raise ValueError('worker_pool with directed sources is not yet qualified; use in_process for source scenarios')
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
    source_path, _ = _interference_input(config, root)
    if source_path is not None:
        config['interference']['sources'] = source_path.relative_to(root).as_posix()
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
        'NATIVE_FAULT_PROFILE': 'none', 'NATIVE_EXPERIMENT_PROFILE': 'urban',
        'NATIVE_EXPERIMENT_PILOT': 1, 'NATIVE_PARAMETER_SNAPSHOTS': 0,
        'NATIVE_COMMAND_PERIOD_S': 1, 'NATIVE_TASK_PERIOD_S': 20,
        'NATIVE_SIONNA_LOG_LEVEL': config['runtime'].get('sionna_log_level', 'info'),
    }
    if config['propagation']['model'] == 'sionna':
        propagation = config['propagation']
        settings.update(NATIVE_RADIO_CONFIG=propagation['radio_config'],
                        NATIVE_RT_GROUND_MATERIAL=propagation['ground_material'],
                        NATIVE_RT_BUILDING_MATERIAL=propagation['building_material'],
                        NATIVE_RT_THICKNESS_M=propagation['thickness_m'])
    settings['NATIVE_URBAN_OUTPUT'] = f'runs/control/{run_id}/generated'
    env.update({'BAS_' + k: str(v) for k, v in settings.items()})
    return env


def control_dir(run_id):
    return ROOT / 'runs/control' / identifier(run_id)


def raw_dir(run_id):
    return ROOT / 'runs/native-radio-realtime' / identifier(run_id)


def snapshot_inputs(config, folder, env):
    """Freeze the exact input files consumed by both host preparation and Docker."""
    write_json(folder / 'config.json', config)
    scene_bytes = (ROOT / config['environment']['scene']).read_bytes()
    (folder / 'scene.json').write_bytes(scene_bytes)
    env['BAS_NATIVE_URBAN_CONFIG'] = (folder / 'scene.json').relative_to(ROOT).as_posix()
    hashes = dict(scene_sha256=hashlib.sha256(scene_bytes).hexdigest(), radio_sha256=None, sources_sha256=None)
    if config['propagation']['model'] == 'sionna':
        radio_bytes = (ROOT / config['propagation']['radio_config']).read_bytes()
        (folder / 'radio.yaml').write_bytes(radio_bytes)
        env['BAS_NATIVE_RADIO_CONFIG'] = (folder / 'radio.yaml').relative_to(ROOT).as_posix()
        hashes['radio_sha256'] = hashlib.sha256(radio_bytes).hexdigest()
    # Source bytes can change after UI validation. Revalidate the exact bytes
    # being frozen and forward only this owned snapshot, never ambient BAS_*.
    env.pop('BAS_NATIVE_SOURCES', None)
    source_path, source_bytes = _interference_input(config, ROOT.resolve())
    if source_path is not None:
        snapshot = folder / 'sources.yaml'
        snapshot.write_bytes(source_bytes)
        env['BAS_NATIVE_SOURCES'] = snapshot.relative_to(ROOT).as_posix()
        hashes['sources_sha256'] = hashlib.sha256(source_bytes).hexdigest()
    return hashes


def recorded_outcome(raw, code, summary, *, source_configured=False):
    """Use backend evidence as well as mission status; never infer success from exit 0."""
    result = {key: summary.get(key, 'not_evaluated') for key in
              ('runtime_status', 'mission_outcome', 'measurement_validity')}
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
                proof = native_rt_worker_contract(stats, scenario)
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
        if folder.exists() or raw.exists():
            raise ValueError('run already exists; choose a new ID')
        for path in ['install/setup.bash', '.external/ns-3-sionna-native/build/scratch/ns3.48-upstream-sionna-tap-spike-default']:
            if not (ROOT / path).is_file():
                raise ValueError(f'missing runtime dependency: {path}; see README')
        subprocess.run(['docker', 'image', 'inspect', config['runtime']['image']],
                       check=True, stdout=subprocess.DEVNULL)
        folder.mkdir(parents=True)
        env = environment(config, run_id)
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
            with (folder / 'runtime.log').open('x') as log:
                child = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True, pass_fds=(lock.fileno(),))
                code = child.wait()
            summary_path = raw / 'metrics/relay_experiment_summary.json'
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
            status = summary.get('status', 'failed') if code == 0 else 'failed'
            record.update(status=status, returncode=code, mission_error=summary.get('error'))
            record.update(recorded_outcome(raw, code, summary,
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
                                      and record['mission_outcome'] == 'succeeded'
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


def main():
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
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'BAS: {exc}', file=sys.stderr)
        sys.exit(2)
