#!/usr/bin/env python3
"""Offline Sionna RT1.2 RF snapshots, not MAC/packet/service availability maps.

Uses the existing prepared scene, radio profile and resolved native emitters.
No propagation or renderer is implemented here. Outputs linear SI arrays and
explicit sampling masks; a Monte Carlo zero does not prove absence of a path.
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
    manifest_path = paths['scene_xml'].parent / 'scene-manifest.json'
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


def execute(config, output, *, root=ROOT, variant='cuda_ad_mono_polarized'):
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
        service_classes='not_evaluated_missing_modem_MAC_and_customer_SLA', variant=variant)
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
        return metadata
    except Exception as error:
        metadata.update(status='failed', error=f'{type(error).__name__}: {error}', wall_s=time.perf_counter() - started)
        write('metadata.json', metadata)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--variant', choices=('cuda_ad_mono_polarized', 'llvm_ad_mono_polarized'), default='cuda_ad_mono_polarized')
    args = parser.parse_args()
    config = load_json(args.config)
    if args.validate_only:
        validate(config)
        print('valid')
    else:
        print(json.dumps(execute(config, args.output, variant=args.variant), allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, ImportError, KeyError, TypeError) as error:
        print(f'Radio maps: {error}', file=sys.stderr)
        raise SystemExit(2)
