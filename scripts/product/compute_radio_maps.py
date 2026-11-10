#!/usr/bin/env python3
"""Offline Sionna RT1.2 RF snapshots and separately audited service observations.

Uses the existing prepared scene, radio profile and resolved native emitters.
No propagation or renderer is implemented here. Outputs linear SI arrays and
explicit sampling masks; a Monte Carlo zero does not prove absence of a path.
Service overlays re-audit actual offered packets; RF values never qualify a class.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import shutil
import sys
import time
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.product.prepare_urban_gazebo import read_rt_radio_profile
from scripts.product.urban_mission import UrbanScenario

EMITTER_FIELDS = {'id', 'position_m', 'orientation_rad', 'pattern', 'gain_dbi',
                  'center_hz', 'bandwidth_hz', 'power_w'}
MAX_POINT_GRID_CELLS = 256  # Explicit software guard, not a performance qualification.


def fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f'{label} requires exactly {sorted(expected)}')


def finite(value, label, minimum=None, positive=False):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and (minimum is None or value >= minimum) and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f'invalid {label}')
    return value


def vector(value, count, label, positive=False):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f'{label} requires {count} coordinates')
    return [finite(v, label, positive=positive) for v in value]


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON field: {key}')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=pairs,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError(f'non-finite JSON: {v}')))


def inside(root, name):
    if not isinstance(name, str) or not name:
        raise ValueError('input path must be a nonempty workspace path')
    path = (root / name.replace('\\', '/')).resolve()
    path.relative_to(root.resolve())
    if not path.is_file():
        raise ValueError(f'input file is missing: {name}')
    return path


def check_emitter(item):
    if not isinstance(item, dict):
        raise ValueError('emitter must be an object')
    if not isinstance(item.get('id'), str) or not item['id']:
        raise ValueError('emitter requires a nonempty identity')
    vector(item['position_m'], 3, 'emitter position')
    angles = vector(item['orientation_rad'], 2, 'azimuth/downtilt')
    if any(abs(v) > math.pi for v in angles) or item['pattern'] not in ('iso', 'tr38901'):
        raise ValueError('unsupported emitter pattern or angles outside [-pi,pi]')
    for name in ('center_hz', 'bandwidth_hz', 'power_w'):
        finite(item[name], name, positive=True)
    finite(item['gain_dbi'], 'gain_dbi')
    try:
        finite(item['power_w'] * 10 ** (item['gain_dbi'] / 10), 'power after feed gain', positive=True)
    except OverflowError:
        raise ValueError('power after feed gain is not representable') from None
    if item['center_hz'] <= item['bandwidth_hz'] / 2:
        raise ValueError('emitter spectrum must have positive frequencies')


def overlap_fraction(emitter, receiver):
    """Exact overlap for the declared flat TX PSD and rectangular RX filter."""
    lo = max(emitter['center_hz'] - emitter['bandwidth_hz'] / 2,
             receiver['center_hz'] - receiver['bandwidth_hz'] / 2)
    hi = min(emitter['center_hz'] + emitter['bandwidth_hz'] / 2,
             receiver['center_hz'] + receiver['bandwidth_hz'] / 2)
    return max(0., hi - lo) / emitter['bandwidth_hz']


def active_at(segment, timestamp):
    # Same positive Time rounding and unchanged 1 ns resolution as native ns-3.
    tick = lambda v: math.floor(v * 1e9 + .5)
    now, start, stop, period = (tick(v) for v in (timestamp, segment['start_s'], segment['stop_s'], segment['period_s']))
    width = math.floor(period * segment['duty_cycle'] + .5)
    return start <= now < stop and (now - start) % period < width


def validate(config, root=ROOT):
    fields(config, {'schema_version', 'mission_scene', 'scene_xml', 'radio_config', 'sources',
                    'desired', 'receiver', 'map'}, 'map configuration')
    if type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('map schema_version must be 1')
    paths = {key: inside(root, config[key]) for key in ('mission_scene', 'scene_xml', 'radio_config')}
    if paths['scene_xml'].name != 'scene.xml':
        raise ValueError('scene_xml must refer to the prepared scene.xml')
    mission = UrbanScenario.load(paths['mission_scene'], root=root)
    profile = read_rt_radio_profile(paths['radio_config'], mission.raw)
    if mission.terrain_bundle_path is not None and paths['scene_xml'] != mission.terrain_bundle_path / 'scene.xml':
        raise ValueError('map and mission must use the same terrain scene')
    manifest_path = inside(root, str(paths['scene_xml'].parent / 'scene-manifest.json'))
    manifest = load_json(manifest_path)
    if manifest.get('coordinate_frame') != 'ENU' or manifest.get('units') != 'm':
        raise ValueError('scene requires the prepared ENU/metre manifest')
    digest = hashlib.sha256(paths['scene_xml'].read_bytes()).hexdigest()
    expected = manifest.get('scene_sha256') or manifest.get('files', {}).get('scene.xml', {}).get('sha256')
    if expected != digest:
        raise ValueError('scene XML hash does not match the prepared manifest')
    desired, receiver, grid = config['desired'], config['receiver'], config['map']
    fields(desired, EMITTER_FIELDS, 'desired transmitter')
    check_emitter(desired)
    if desired['center_hz'] != profile['radio']['carrier_hz']:
        raise ValueError('desired frequency must match the shared scene/radio profile')
    fields(receiver, {'center_hz', 'bandwidth_hz', 'temperature_k', 'noise_figure_db'}, 'receiver')
    for key in ('center_hz', 'bandwidth_hz', 'temperature_k'):
        finite(receiver[key], key, positive=True)
    finite(receiver['noise_figure_db'], 'noise_figure_db', minimum=0)
    try:
        finite(1.3803e-23 * receiver['temperature_k'] * receiver['bandwidth_hz']
               * 10 ** (receiver['noise_figure_db'] / 10), 'derived noise power', positive=True)
    except OverflowError:
        raise ValueError('derived noise power is not representable') from None
    if receiver['center_hz'] <= receiver['bandwidth_hz'] / 2 or overlap_fraction(desired, receiver) == 0:
        raise ValueError('receiver band must be positive and overlap the desired signal')
    fields(grid, {'method', 'center_m', 'size_m', 'cell_size_m', 'time_s', 'direction', 'samples_per_tx', 'point_cells'}, 'map')
    if grid['method'] not in ('radio_map', 'point_grid'):
        raise ValueError('map method must be radio_map or point_grid')
    vector(grid['center_m'], 3, 'map center')
    sizes = vector(grid['size_m'], 2, 'map size', positive=True)
    cells = vector(grid['cell_size_m'], 2, 'map cell size', positive=True)
    counts = [size / cell for size, cell in zip(sizes, cells)]
    if any(round(v) < 1 or not math.isclose(v, round(v), rel_tol=0, abs_tol=1e-9) for v in counts):
        raise ValueError('Sionna1.2 map size must be an exact multiple of cell_size')
    if grid['method'] == 'point_grid' and math.prod(round(v) for v in counts) > MAX_POINT_GRID_CELLS:
        raise ValueError(f'point_grid software limit is {MAX_POINT_GRID_CELLS} receivers; no runtime performance guarantee')
    finite(grid['time_s'], 'snapshot time', minimum=0)
    if not isinstance(grid['direction'], str) or not grid['direction'].strip():
        raise ValueError('explicit link direction label is required')
    if type(grid['samples_per_tx']) is not int or not 1 <= grid['samples_per_tx'] <= 2**32 - 1:
        raise ValueError('samples_per_tx must be a positive uint32')
    if not isinstance(grid['point_cells'], list):
        raise ValueError('point_cells must be a list of [row,column]')
    for pair in grid['point_cells']:
        if (not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not int for v in pair)
                or not 0 <= pair[0] < round(counts[1]) or not 0 <= pair[1] < round(counts[0])):
            raise ValueError('point check cell is outside the map')
    emitters = []
    if config['sources'] is not None:
        paths['sources'] = inside(root, config['sources'])
        resolved = load_json(paths['sources'])
        if (not isinstance(resolved, dict) or resolved.get('schema_version') != 1 or resolved.get('clock') != 'ns3_simulation_seconds'
                or not isinstance(resolved.get('segments'), list)):
            raise ValueError('sources must be resolved prepare_native_sources JSON, not another schedule format')
        # The producer owns sweep/duration expansion. Only validate quantities
        # consumed here; no second schedule generator or propagation model.
        for segment in resolved['segments']:
            check_emitter(segment)
            for key in ('start_s', 'stop_s'):
                finite(segment[key], key, minimum=0)
            finite(segment['period_s'], 'period_s', minimum=1e-4)
            finite(segment['duty_cycle'], 'duty_cycle', positive=True)
            if segment['stop_s'] <= segment['start_s'] or segment['duty_cycle'] > 1:
                raise ValueError('invalid resolved native source interval/duty')
            emitters.append(segment)
    from scripts.product.prepare_sionna_scene import ITU_FREQUENCIES_GHZ
    material_types = [item.get('value') for item in ET.parse(paths['scene_xml']).findall(".//bsdf/string[@name='type']")]
    for tx in [desired, *emitters]:
        for material in material_types:
            if material not in ITU_FREQUENCIES_GHZ or not any(
                    lo <= tx['center_hz'] / 1e9 <= hi for lo, hi in ITU_FREQUENCIES_GHZ[material]):
                raise ValueError('an emitter frequency is outside the prepared ITU material validity range')
    frame = mission.raw['frame']
    for key in ('origin_latitude_deg', 'origin_longitude_deg', 'origin_elevation_m', 'spherical_heading_deg'):
        finite(frame[key], key)
    return paths, mission, profile, emitters


def combine(desired, interference, noise_w):
    """Power arithmetic only; propagation comes exclusively from RadioMapSolver."""
    finite(noise_w, 'noise power', positive=True)
    signal = np.asarray(desired, dtype=np.float64)
    jammer = np.asarray(interference, dtype=np.float64)
    if (signal.shape != jammer.shape or not np.isfinite(signal).all() or not np.isfinite(jammer).all()
            or (signal < 0).any() or (jammer < 0).any()):
        raise ValueError('solver powers must be finite nonnegative arrays of matching shape')
    valid = signal > 0
    js = np.zeros_like(signal)
    np.divide(jammer, signal, out=js, where=valid)
    # Explicit mask identifies undefined J/S; no NaN/Infinity sentinel or outage claim.
    return dict(rss_desired_w=signal, interference_w=jammer, sinr_linear=signal / (jammer + noise_w),
                js_linear=js, desired_sampled_positive=valid, js_defined=valid)


def grid_centers(grid):
    count = [round(size / step) for size, step in zip(grid['size_m'], grid['cell_size_m'])]
    x, y = [center - size / 2 + (np.arange(n) + .5) * step for center, size, step, n in
            zip(grid['center_m'], grid['size_m'], grid['cell_size_m'], count)]
    xx, yy = np.meshgrid(x, y)
    return np.stack((xx, yy, np.full_like(xx, grid['center_m'][2])), axis=-1)


def cfr_power(paths, frequency):
    # Physical frequencies are baseband offsets; 0 is the TX carrier. Current
    # SDK absolute-center double-carrier convention is diagnostic only.
    h = np.asarray(paths.cfr(frequencies=[frequency], normalize_delays=True,
                             normalize=False, out_type='numpy'))
    if h.ndim != 6 or h.shape[1:] != (1, 1, 1, 1, 1):
        raise ValueError(f'expected single-element/stream Sionna CFR, got {h.shape}')
    return np.abs(h[:, 0, 0, 0, 0, 0]) ** 2


def render(output):
    """Render an existing completed snapshot without modifying its NPZ data."""
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    from matplotlib.colors import ListedColormap
    output = Path(output)
    metadata = load_json(output / 'metadata.json')
    if metadata.get('status') != 'completed':
        raise ValueError('plot requires a completed RF snapshot')
    grid, origin = metadata['map'], metadata['origin']
    x, y, z = grid['center_m']; width, height = grid['size_m']
    extent = (x-width/2, x+width/2, y-height/2, y+height/2)
    with np.load(output / 'maps.npz', allow_pickle=False) as stored:
        data = {name: stored[name] for name in stored.files}
    desired = data['desired_sampled_positive']
    resolved = desired & data['all_computed_tx_sampled_positive']
    metrics = [('RSS', 'dBm', 'rss_desired_w', desired, 30),
               ('SINR', 'dB', 'sinr_linear', resolved, 0),
               ('J/S', 'dB', 'js_linear', resolved & data['js_defined'], 0)]
    figure, axes = plt.subplots(1, 3, figsize=(16, 5), sharex=True, sharey=True)
    cmap = matplotlib.colormaps['viridis'].copy(); cmap.set_bad('#999999')
    missing = {}
    try:
        for axis, (name, unit, key, mask, offset) in zip(axes, metrics):
            values = data[key]
            valid = mask & np.isfinite(values) & (values > 0)
            db = np.ma.masked_all(values.shape)
            db[valid] = 10 * np.log10(values[valid]) + offset
            image = axis.imshow(db, extent=extent, origin='lower', interpolation='nearest', cmap=cmap)
            if valid.any():
                figure.colorbar(image, ax=axis, label=unit, shrink=.8)
            if name == 'J/S':
                zero = mask & np.isfinite(values) & (values == 0)
                white = ListedColormap(['white']); white.set_bad((0, 0, 0, 0))
                axis.imshow(np.ma.masked_where(~zero, np.ones(values.shape)), extent=extent,
                            origin='lower', interpolation='nearest', cmap=white, vmin=0, vmax=1)
                valid |= zero  # Known zero J is -inf dB, distinct from missing samples.
            missing[name] = int((~valid).sum())
            axis.set(title=f'{name} [{unit}]', xlabel='World ENU x [m]', facecolor='#999999')
        axes[0].set_ylabel('World ENU y [m]')
        figure.suptitle(f"{grid['direction']} | t={grid['time_s']:g} s | ENU Z={z:g} m\n"
                       f"{metadata['map_kind']} | RF snapshot, not service availability", fontsize=11)
        figure.text(.5, .065, f"ENU origin: {origin['origin_latitude_deg']:g} deg, "
                    f"{origin['origin_longitude_deg']:g} deg, {origin['origin_elevation_m']:g} m", ha='center', fontsize=9)
        figure.text(.5, .025, 'Gray: no data / sampled zero, not outage. White in J/S: J=0 (-inf dB).', ha='center', fontsize=9)
        figure.tight_layout(rect=(0, .18, 1, .87))
        figure.savefig(output / 'maps.png', dpi=150)
    finally:
        plt.close(figure)
    metadata['plot'] = dict(file='maps.png', matplotlib_version=matplotlib.__version__, backend='Agg',
        sha256=hashlib.sha256((output / 'maps.png').read_bytes()).hexdigest(),
        source_npz_sha256=hashlib.sha256((output / 'maps.npz').read_bytes()).hexdigest(),
        extent_enu_m=list(extent), origin='lower', no_data_cells=missing)
    temporary = output / 'metadata.tmp'
    temporary.write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(output / 'metadata.json')
    return metadata['plot']


def execute(config, output, *, root=ROOT, variant='cuda_ad_mono_polarized', plot=False):
    paths, mission, profile, segments = validate(config, root)
    output = output.resolve()
    output.relative_to(root.resolve())
    output.mkdir(parents=True, exist_ok=False)
    write = lambda name, value: (output / name).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    write('input-config.json', config)
    files = {name: dict(path=path.relative_to(root).as_posix(), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
             for name, path in paths.items()}
    geometry = output / 'scene'
    geometry.mkdir()
    # Freeze only the scene and its declared local meshes; never a whole checkout.
    members = {'scene.xml', 'scene-manifest.json'}
    if mission.terrain_manifest is not None:
        members.update(mission.terrain_manifest['files'])
    tree = ET.parse(paths['scene_xml'])
    members.update(item.get('value') for item in tree.findall(".//string[@name='filename']"))
    for relative in members:
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts or '\\' in relative:
            raise ValueError('prepared scene assets must be local relative files')
        source = (paths['scene_xml'].parent / relative).resolve()
        source.relative_to(paths['scene_xml'].parent)
        target = geometry / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files['scene/' + relative] = dict(sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    for name, path in paths.items():
        shutil.copy2(path, output / (name + path.suffix))
    frozen = copy.deepcopy(config)
    for name in ('mission_scene', 'radio_config', 'sources'):
        if name in paths:
            frozen[name] = (output / (name + paths[name].suffix)).relative_to(root).as_posix()
    frozen['scene_xml'] = (geometry / 'scene.xml').relative_to(root).as_posix()
    if mission.terrain_bundle_path is not None:
        frozen_mission = copy.deepcopy(mission.raw)
        frozen_mission['terrain_bundle'] = geometry.relative_to(root).as_posix()
        write('mission_scene.json', frozen_mission)
    write('config.json', frozen)
    validate(frozen, root)
    metadata = dict(status='running', inputs=files, origin=mission.raw['frame'], map=config['map'],
        desired=config['desired'], receiver=config['receiver'], solver_profile=profile['sionna'], source_states=[],
        assumptions=[
            'Continuous full-buffer desired RF snapshot; MAC airtime, queues, retries and packet SLA are not modeled. Not service availability.',
            'Flat transmit PSD, ideal rectangular receive filter; exact spectral overlap, propagation evaluated at each TX center frequency.',
            'Independent transmitters add in linear power; source activity is instantaneous at explicit ns-3 snapshot time, not averaged duty power.',
            'RadioMapSolver method integrates cell-averaged noncoherent path energy for an ideal isotropic receiver; point_grid and checks instead use the runtime polarized iso V receiver.',
            'Map zero means sampled_zero_or_no_path; it is not evidence of outage. J/S is undefined where desired_sampled_positive is false.',
            'tr38901 TX: unnormalized OUTDOOR pattern, 65deg HPBW/30dB attenuation/8dBi peak, matching ns-3.48 element power pattern; feed gain is additional once.',
            'Fixed horizontal ENU Z plane, not terrain-following AGL. Explicit TX positions are snapshot inputs; no trajectory interpolation.',
            'Point checks are diagnostic: cell averages and point values need not agree; no physical calibration or acceptance error threshold is claimed.'],
        service_classes='not_evaluated_requires_qualified_packet_load_series', variant=variant)
    metadata['assumptions'].extend([
        'point_grid uses actual iso V polarized PathSolver receivers at cell centers, not a cell area average; all devices are stationary at the snapshot.',
        'point_grid/CFR uses baseband offset zero, the physical TX center. A single center-frequency gain approximates the overlapping band; native multiband equivalence is unqualified.',
        'Legacy SDK absolute-frequency CFR is retained only as an unqualified point diagnostic: CIR already contains carrier phase before its additional absolute-band phase.'])
    metadata['map_kind'] = ('unpolarized_cell_average_RF_diagnostic' if config['map']['method'] == 'radio_map'
                            else 'polarized_point_grid_carrier_RF_diagnostic')
    metadata['code_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write('metadata.json', metadata)
    started = time.perf_counter()
    try:
        versions = {name: importlib.metadata.version(name) for name in ('sionna-rt', 'mitsuba', 'drjit')}
        metadata['versions'] = versions
        if versions != {'sionna-rt': '1.2.0', 'mitsuba': '3.7.1', 'drjit': '1.2.0'}:
            raise ValueError(f'unsupported map runtime versions: {versions}')
        import mitsuba as mi
        mi.set_variant(variant)
        from sionna.rt import load_scene, PlanarArray, Transmitter, Receiver, RadioMapSolver, PathSolver
        scene = load_scene(str(geometry / 'scene.xml'), merge_shapes=True)
        scene.rx_array = PlanarArray(num_rows=1, num_cols=1, pattern='iso', polarization='V')
        grid = config['map']
        flags = {name: profile['sionna'][name] for name in ('max_depth', 'los', 'specular_reflection',
                    'diffuse_reflection', 'refraction', 'diffraction', 'edge_diffraction', 'seed')}
        solver = RadioMapSolver()
        point_solver = PathSolver()
        controls = []
        powers = []
        centers = None
        desired_overlap = overlap_fraction(config['desired'], config['receiver'])
        transmitters = [(config['desired'], desired_overlap, True)]
        for segment in segments:
            overlap = overlap_fraction(segment, config['receiver'])
            active = active_at(segment, grid['time_s'])
            metadata['source_states'].append(dict(id=segment['id'], segment=segment,
                active=active, spectral_overlap_fraction=overlap, computed=active and overlap > 0))
            if active and overlap > 0:
                transmitters.append((segment, overlap, False))
        for index, (tx, overlap, desired) in enumerate(transmitters):
            scene.frequency = tx['center_hz']
            scene.tx_array = PlanarArray(num_rows=1, num_cols=1, pattern=tx['pattern'], polarization='V')
            scene.add(Transmitter(name='map_tx', position=tx['position_m'], orientation=[*tx['orientation_rad'], 0],
                                 power_dbm=10 * math.log10(tx['power_w'] * 1000) + tx['gain_dbi']))
            feed_w = tx['power_w'] * 10 ** (tx['gain_dbi'] / 10) * overlap
            if grid['method'] == 'radio_map':
                result = solver(scene, center=grid['center_m'], orientation=[0, 0, 0], size=grid['size_m'],
                                cell_size=grid['cell_size_m'], samples_per_tx=grid['samples_per_tx'], **flags)
                rss = np.asarray(result.rss.numpy(), dtype=np.float64)[0] * overlap
                if centers is None:
                    centers = result.cell_centers.numpy()
            else:
                centers = grid_centers(grid)
                for number, position in enumerate(centers.reshape(-1, 3)):
                    scene.add(Receiver(name=f'grid_rx_{number}', position=position.tolist(), orientation=[0, 0, 0]))
                paths_found = point_solver(scene, samples_per_src=grid['samples_per_tx'],
                    max_num_paths_per_src=profile['sionna']['max_num_paths_per_src'],
                    synthetic_array=profile['sionna']['synthetic_array'], **flags)
                rss = cfr_power(paths_found, 0).reshape(centers.shape[:2]) * feed_w
                for number in range(centers.shape[0] * centers.shape[1]):
                    scene.remove(f'grid_rx_{number}')
            powers.append(rss)
            for row, column in grid['point_cells']:
                scene.add(Receiver(name='map_probe', position=centers[row, column].tolist(), orientation=[0, 0, 0]))
                paths_found = point_solver(scene, samples_per_src=profile['sionna']['samples_per_src'],
                    max_num_paths_per_src=profile['sionna']['max_num_paths_per_src'],
                    synthetic_array=profile['sionna']['synthetic_array'], **flags)
                real, imag = (np.asarray(part.numpy(), dtype=np.float64) for part in paths_found.a)
                controls.append(dict(tx_index=index, tx_id=tx['id'], desired=desired, row=row, column=column,
                    position_m=centers[row, column].tolist(), map_rss_w=float(rss[row, column]),
                    point_incoherent_rss_w=float(np.sum(real**2 + imag**2) * feed_w),
                    point_coherent_at_carrier_rss_w=float(cfr_power(paths_found, 0)[0] * feed_w),
                    point_sdk_legacy_center_unqualified_rss_w=float(cfr_power(paths_found, tx['center_hz'])[0] * feed_w)))
                scene.remove('map_probe')
            scene.remove('map_tx')
        # Match configured ns-3 reference thermal constant, temperature, NF and band.
        receiver = config['receiver']
        noise = 1.3803e-23 * receiver['temperature_k'] * receiver['bandwidth_hz'] * 10 ** (receiver['noise_figure_db'] / 10)
        interference = np.sum(powers[1:], axis=0) if len(powers) > 1 else np.zeros_like(powers[0])
        arrays = combine(powers[0], interference, noise)
        arrays['cell_centers_enu_m'] = centers
        arrays['computed_tx_inband_rss_w'] = np.stack(powers)
        arrays['all_computed_tx_sampled_positive'] = (np.stack(powers) > 0).all(axis=0)
        np.savez_compressed(output / 'maps.npz', **arrays)
        write('point_checks.json', controls)
        metadata.update(status='completed', versions=versions, noise_w=noise, noise_boltzmann_j_per_k=1.3803e-23,
                        wall_s=time.perf_counter() - started,
                        tx_order=[dict(id=t['id'], desired=d, spectral_overlap_fraction=o) for t, o, d in transmitters],
                        zero_desired_cells=int((~arrays['desired_sampled_positive']).sum()))
        write('metadata.json', metadata)
    except Exception as error:
        metadata.update(status='failed', error=f'{type(error).__name__}: {error}', wall_s=time.perf_counter() - started)
        write('metadata.json', metadata)
        raise
    if plot:
        metadata['plot'] = render(output)
    return metadata


SERVICE_STATUS = {"not_measured": 0, "invalid": 1, "insufficient_evidence": 2,
                  "measured_fail": 3, "measured_pass": 4}


def _stream_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _constant_source_state(segment, start_s, stop_s, expected):
    """Check the already-resolved schedule; never average a pulsed source."""
    tick = lambda v: math.floor(v * 1e9 + .5)
    a, b, first, last, period = map(tick, (start_s, stop_s, segment["start_s"],
                                         segment["stop_s"], segment["period_s"]))
    width = math.floor(period * segment["duty_cycle"] + .5)
    if b <= a or width <= 0:
        return not expected
    if expected:
        if not first <= a < b <= last:
            return False
        return width >= period or (a-first)//period == (b-1-first)//period and (b-1-first)%period < width
    low, high = max(a, first), min(b, last)
    if high <= low:
        return True
    cycle = max(0, (low-first)//period)
    on = first + cycle*period
    if on+width <= low:
        on += period
    return on >= high


def _service_run(run_id, root):
    """Re-audit raw payload identity/deadlines and the recorded native clock."""
    from scripts.product.analyze_relay_experiment import analyze_payload_lineage, runtime_timing_evidence
    from scripts.product.summarize_native_radio_five_uav import iter_native_events
    if not isinstance(run_id, str) or not run_id or any(c not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
        raise ValueError("service evidence requires a simple immutable run ID")
    native = root / "runs/native-radio-realtime" / run_id
    meta = load_json(root / "runs/control" / run_id / "run.json")
    mission = load_json(native / "metrics/relay_experiment_summary.json")
    stats = load_json(native / "metrics/native_radio_stats.json")
    if (meta.get("run_id") != run_id or meta.get("measurement_validity") != "valid"
            or meta.get("runtime_status") != "completed" or meta.get("source_dirty") is not False
            or meta.get("runtime_verification_errors") != []):
        raise ValueError("run is not an unchanged verified valid runtime")
    for label in ("node_state_journal_verification", "native_rt_worker_verification"):
        if meta.get(label, {}).get("passed") is not True:
            raise ValueError("closed native/pose evidence missing: " + label)
    start, end = mission.get("measurement_start_host_monotonic_ns"), mission.get("measurement_end_host_monotonic_ns")
    if type(start) is not int or type(end) is not int or not start < end:
        raise ValueError("exact recorded host observation window missing")
    pose, lag = {}, []
    capture = native / "logs/native_radio_events.csv"
    for row in iter_native_events(capture, event_names={"live_pose", "realtime_lag"}):
        if row["event"] == "realtime_lag":
            lag.append({key: row[key] for key in ("event", "wall_monotonic_ns", "time_s", "value")})
        elif start <= row["wall_monotonic_ns"] < end:
            node = row["node"]
            stamp = row["wall_monotonic_ns"]
            xyz = np.array([row["x"], row["y"], row["z"]], dtype=float)
            if not np.isfinite(xyz).all() or row["value"] is None or not 0 <= row["value"] <= 100:
                raise ValueError("unqualified applied native pose age/coordinates")
            item = pose.setdefault(node, dict(first_ns=stamp, last_ns=stamp, samples=0,
                gap_max_ns=stamp-start, minimum=xyz.copy(), maximum=xyz.copy(),
                native_min_s=row["time_s"], native_max_s=row["time_s"]))
            if item["samples"] and stamp <= item["last_ns"]:
                raise ValueError("applied pose host clock is not strictly increasing")
            item["gap_max_ns"] = max(item["gap_max_ns"], stamp-item["last_ns"])
            item["last_ns"] = stamp
            item["samples"] += 1
            item["minimum"] = np.minimum(item["minimum"], xyz)
            item["maximum"] = np.maximum(item["maximum"], xyz)
            item["native_min_s"] = min(item["native_min_s"], row["time_s"])
            item["native_max_s"] = max(item["native_max_s"], row["time_s"])
    for item in pose.values():
        item["gap_max_ns"] = max(item["gap_max_ns"], end-item["last_ns"])
    timing = runtime_timing_evidence(mission, lag, stats)
    lineage = analyze_payload_lineage(native, start, end, complete=timing["eligible"])
    if not timing["eligible"]:
        lineage["measurement_eligible"] = False
    artifacts = [dict(path=str(path.relative_to(root)), bytes=path.stat().st_size, sha256=_stream_sha(path))
        for path in (root/"runs/control"/run_id/"run.json", native/"metrics/relay_experiment_summary.json",
                     native/"metrics/native_radio_stats.json")]
    return dict(native=native, meta=meta, stats=stats, mission=mission, pose=pose,
                lineage=lineage, timing=timing, capture_sha256=_stream_sha(capture), artifacts=artifacts)


def service_maps(snapshot, evidence, output, *, profile_path=None, root=ROOT, plot=False):
    """Attach observed transport-class windows to a spatial snapshot.

    Unknown cells stay unknown. A cell stores observations inside the explicitly
    declared position box; it does not extrapolate to the rest of its area.
    """
    from network.coordination.service_profiles import load_service_profiles, qualify_payload_services
    snapshot, output, root = Path(snapshot).resolve(), Path(output).resolve(), Path(root).resolve()
    snapshot.relative_to(root); output.relative_to(root)
    snapshot_files = {name: inside(root, str(snapshot / name)) for name in ("metadata.json", "maps.npz")}
    metadata = load_json(snapshot_files["metadata.json"])
    if metadata.get("status") != "completed":
        raise ValueError("service maps require a completed RF snapshot")
    fields(evidence, {"schema_version", "snapshot_metadata_sha256", "snapshot_npz_sha256",
        "uav_count", "target_uav_id", "direction", "peer_positions_m", "position_tolerance_m",
        "bindings"}, "service-map evidence")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1:
        raise ValueError("service-map evidence schema must be integer 1")
    for name, key in (("metadata.json", "snapshot_metadata_sha256"), ("maps.npz", "snapshot_npz_sha256")):
        if _stream_sha(snapshot_files[name]) != evidence[key]:
            raise ValueError("RF snapshot hash mismatch: " + name)
    count, target = evidence["uav_count"], evidence["target_uav_id"]
    if type(count) is not int or type(target) is not int or not 1 <= target <= count <= 5:
        raise ValueError("service map requires a target inside a 1..5 roster")
    if evidence["direction"] != "gcs_to_uav" or metadata["desired"]["id"] != "cp":
        raise ValueError("only measured CP to UAV payload direction is supported")
    tolerance = np.array(vector(evidence["position_tolerance_m"], 3, "position tolerance"))
    if (tolerance < 0).any() or any(tolerance[i] > metadata["map"]["cell_size_m"][i]/2 for i in range(2)):
        raise ValueError("horizontal position tolerance must fit inside one cell")
    peers = evidence["peer_positions_m"]
    required = {"cp", *(f"uav{i}" for i in range(1,count+1) if i != target)}
    fields(peers, required, "fixed peer positions")
    for name, point in peers.items():
        vector(point, 3, name + " fixed peer position")
    if peers["cp"] != metadata["desired"]["position_m"]:
        raise ValueError("fixed CP differs from the RF snapshot transmitter")
    if not isinstance(evidence["bindings"], list):
        raise ValueError("service bindings must be an explicit list")
    profiles = load_service_profiles(profile_path)
    names = list(profiles["config"]["profiles"])
    with np.load(snapshot_files["maps.npz"], allow_pickle=False) as data:
        centers = np.array(data["cell_centers_enu_m"], copy=True)
    if centers.ndim != 3 or centers.shape[-1] != 3 or not np.isfinite(centers).all():
        raise ValueError("RF snapshot lacks a rectangular ENU grid")
    statuses = np.zeros((len(names), *centers.shape[:2]), dtype=np.uint8)
    observations = np.zeros_like(statuses, dtype=np.uint32)
    rows, all_statuses, seen = [], {}, set()
    cached_id, cached_run = None, None
    for binding in evidence["bindings"]:
        fields(binding, {"row", "column", "run_id"}, "service binding")
        row, col = binding["row"], binding["column"]
        if (type(row) is not int or type(col) is not int or not 0 <= row < centers.shape[0]
                or not 0 <= col < centers.shape[1]):
            raise ValueError("service binding outside RF grid")
        identity = (row, col, binding["run_id"])
        if identity in seen:
            raise ValueError("duplicate cell/run binding")
        seen.add(identity)
        detail = dict(binding, status="invalid", errors=[])
        selected_class = None
        try:
            run_id = binding["run_id"]
            if run_id != cached_id:
                cached_run = _service_run(run_id, root)
                cached_id = run_id
            observed = cached_run
            native, meta, stats = observed["native"], observed["meta"], observed["stats"]
            lineage = observed["lineage"]
            selected_class = lineage.get("offered_load", {}).get("profile", {}).get("class")
            if (stats.get("uav_count") != count or stats.get("sionna_solver_enabled") is not True
                    or stats.get("sionna_drives_rx_psd") is not True):
                raise ValueError("observed radio/roster differs from the declared RT context")
            if meta.get("radio_sha256") != metadata["inputs"]["radio_config"]["sha256"]:
                raise ValueError("radio profile fingerprint mismatch")
            desired, receiver = metadata["desired"], metadata["receiver"]
            if (desired["pattern"] != "iso" or desired["gain_dbi"] != 0
                    or desired["power_w"] != stats.get("tx_power_w")
                    or any(endpoint["center_hz"] != stats.get("wifi_actual_center_frequency_hz")
                           or endpoint["bandwidth_hz"] != stats.get("wifi_channel_width_mhz", 0)*1e6
                           for endpoint in (desired, receiver))):
                raise ValueError("RF snapshot transmitter or receive band differs from the native radio")
            geometry = load_json(native/"configuration/final/inputs/geometry_source/value.json")
            for key in ("origin_latitude_deg", "origin_longitude_deg", "origin_elevation_m", "spherical_heading_deg"):
                if geometry["frame"][key] != metadata["origin"][key]:
                    raise ValueError("ENU origin mismatch")
            manifest = load_json(native/"configuration/final/inputs/sionna_geometry_manifest/value.json")
            for key, record in metadata["inputs"].items():
                if key.startswith("scene/") and key.endswith((".obj", ".xml")):
                    if manifest["files"][key[6:]]["sha256"] != record["sha256"]:
                        raise ValueError("scene geometry fingerprint mismatch")
            if _stream_sha(native/"configuration/final/inputs/sionna_scene/value.xml") != metadata["inputs"]["scene_xml"]["sha256"]:
                raise ValueError("used Sionna scene XML mismatch")
            sources = metadata["inputs"].get("sources")
            actual_path = native/"logs/native_sources_actual.json"
            if sources:
                if not actual_path.is_file() or _stream_sha(actual_path) != sources["sha256"]:
                    raise ValueError("resolved native source fingerprint mismatch")
            elif actual_path.exists() and load_json(actual_path).get("segments"):
                raise ValueError("RF snapshot omitted configured native sources")
            expected_positions = dict(peers, **{f"uav{target}": centers[row,col].tolist()})
            envelopes = {}
            for node, expected in expected_positions.items():
                pose = observed["pose"].get(node)
                if not pose or pose["samples"] < 2 or pose["gap_max_ns"] > 100_000_000:
                    raise ValueError("applied pose observation incomplete for " + node)
                point = np.array(expected)
                if (pose["minimum"] < point-tolerance).any() or (pose["maximum"] > point+tolerance).any():
                    raise ValueError("run left the declared observed position box: " + node)
                envelopes[node] = dict(minimum_enu_m=pose["minimum"].tolist(), maximum_enu_m=pose["maximum"].tolist())
            native_low = min(p["native_min_s"] for p in observed["pose"].values())
            native_high = max(p["native_max_s"] for p in observed["pose"].values())
            if not native_low <= metadata["map"]["time_s"] <= native_high:
                raise ValueError("RF snapshot time outside observed native window")
            for state in metadata.get("source_states", []):
                if not _constant_source_state(state["segment"], max(0.,native_low-.1), native_high+.1, state["active"]):
                    raise ValueError("source activity changed across the measured class window")
            qualified = qualify_payload_services(lineage, profiles, run_metadata=dict(meta,
                rng_seed=stats.get("rng_seed"), rng_run=stats.get("rng_run"),
                rt_solver_seed=stats.get("sionna_solver", {}).get("seed")))
            if selected_class not in names:
                raise ValueError("no supported offered class observed")
            streams = [s for s in qualified["classes"][selected_class]["streams"]
                       if s["uav_id"] == target and s["direction"] == evidence["direction"]]
            if not streams:
                raise ValueError("no matching audited directional stream")
            result_statuses = [s["status"] for s in streams]
            status = next((s for s in ("invalid","measured_fail","insufficient_evidence")
                           if s in result_statuses), "measured_pass" if all(s=="measured_pass" for s in result_statuses) else "not_measured")
            detail.update(status=status, class_name=selected_class, source_commit=meta["source_commit"],
                qualification=qualified, position_envelopes=envelopes,
                native_window_s=[native_low,native_high], native_capture_sha256=observed["capture_sha256"],
                run_artifacts=observed.get("artifacts", []),
                independent_series_status="insufficient_evidence")
        except (ValueError, KeyError, OSError, TypeError) as error:
            detail["errors"].append(str(error))
        if selected_class in names:
            key = (names.index(selected_class), row, col)
            all_statuses.setdefault(key, []).append(detail["status"])
            observations[key] += 1
        rows.append(detail)
    for key, values in all_statuses.items():
        status = next((s for s in ("invalid","measured_fail","insufficient_evidence") if s in values),
                      "measured_pass" if all(s=="measured_pass" for s in values) else "not_measured")
        statuses[key] = SERVICE_STATUS[status]
    output.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(output/"service_maps.npz", status=statuses, observed_bindings=observations,
                        cell_centers_enu_m=centers)
    result = dict(schema_version=1, status="completed", evidence_kind="measured_transport_observations",
        qualification_scope="observed_single_run_limits_only",
        semantic_scope="synthetic_transport_load_proxy_not_object_or_critical_command_semantics",
        independent_series_status="insufficient_evidence",
        rf_snapshot_equivalence="not_evaluated_observed_transport_overlay_only",
        service_profile_sha256=profiles["sha256"],
        service_profile_id=profiles["config"]["profile_id"], class_order=names, status_codes=SERVICE_STATUS,
        snapshot=str(snapshot.relative_to(root)), snapshot_metadata_sha256=evidence["snapshot_metadata_sha256"],
        snapshot_npz_sha256=evidence["snapshot_npz_sha256"], context=evidence,
        observations=rows, map=metadata["map"], origin=metadata["origin"],
        assumptions=["Only packet lineage determines observed class status; RSS/SINR never imply a passing class.",
            "Unknown cells and unmeasured classes remain not_measured; no spatial or class extrapolation.",
            "A colored cell represents its recorded position envelope, not qualification of its entire area.",
            "Source state must be constant over the observed window with the existing100ms freshness margin.",
            "Physical hardware and independent statistical series are separate qualifications."])
    (output/"input-evidence.json").write_text(json.dumps(evidence,indent=2,allow_nan=False)+"\n")
    (output/"service_metadata.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    if plot:
        render_service_maps(output)
    return result


def render_service_maps(output):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from matplotlib.patches import Patch
    output = Path(output)
    meta = load_json(output/"service_metadata.json")
    with np.load(output/"service_maps.npz", allow_pickle=False) as stored:
        status = np.array(stored["status"], copy=True)
    colors = ["#999999","#b36b00","#4678a5","#b73535","#298f50"]
    cmap = ListedColormap(colors); norm = BoundaryNorm(np.arange(-.5,5.5),5)
    x,y,_ = meta["map"]["center_m"]; w,h = meta["map"]["size_m"]
    fig,axes = plt.subplots(2,3,figsize=(13,8),sharex=True,sharey=True)
    try:
        for i,axis in enumerate(axes.flat):
            axis.imshow(status[i],origin="lower",extent=(x-w/2,x+w/2,y-h/2,y+h/2),cmap=cmap,norm=norm,interpolation="nearest")
            axis.set(title=meta["class_order"][i],xlabel="ENU x [m]",ylabel="ENU y [m]")
        fig.suptitle("Observed transport-class samples; engineering profile; no area extrapolation")
        fig.legend(handles=[Patch(color=colors[v],label=k) for k,v in SERVICE_STATUS.items()],
                   loc="lower center",ncol=5)
        fig.tight_layout(rect=(0,.07,1,.95));fig.savefig(output/"service_maps.png",dpi=150)
    finally:
        plt.close(fig)
    return output/"service_maps.png"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service-map-from', type=Path, help='Attach observed classes to an existing completed RF snapshot; CPU only')
    parser.add_argument('--service-evidence', type=Path, help='Strict cell/run binding JSON for --service-map-from')
    parser.add_argument('--service-profile', type=Path, help='Versioned engineering profile; default service_profiles_v1.json')

    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--plot', action='store_true', help='also render RSS/SINR/J/S PNG using Matplotlib Agg')
    parser.add_argument('--variant', choices=('cuda_ad_mono_polarized', 'llvm_ad_mono_polarized'), default='cuda_ad_mono_polarized')
    args = parser.parse_args()
    if args.validate_only and args.plot:
        parser.error('--plot requires computation; omit --validate-only')
    if args.service_map_from:
        if args.config or args.validate_only or args.service_evidence is None:
            parser.error('--service-map-from requires --service-evidence and excludes --config/--validate-only')
        result = service_maps(args.service_map_from, load_json(args.service_evidence), args.output,
                              profile_path=args.service_profile, plot=args.plot)
        print(json.dumps(dict(status=result['status'], service_profile_sha256=result['service_profile_sha256'])))
        return
    if args.config is None or args.service_evidence is not None or args.service_profile is not None:
        parser.error('RF computation requires --config; service arguments require --service-map-from')
    config = load_json(args.config)
    if args.validate_only:
        validate(config)
        print('valid')
    else:
        print(json.dumps(execute(config, args.output, variant=args.variant, plot=args.plot), allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, ImportError, KeyError, TypeError) as error:
        print(f'Radio maps: {error}', file=sys.stderr)
        raise SystemExit(2)
