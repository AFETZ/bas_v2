#!/usr/bin/env python3
"""Prepare one procedural terrain OBJ for both Gazebo and Sionna, without RT.

Requires numpy and trimesh==4.10.1 (MIT), no optional trimesh extras. --example
prints a configurable synthetic 10 km reference; it is not customer terrain.
GZ_SIM_RESOURCE_PATH must contain the output directory. Flight/AGL integration
and Gazebo collision / Sionna runtime qualification are separate checks.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import numbers
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.product.prepare_sionna_scene import ITU_FREQUENCIES_GHZ

TRIMESH_VERSION = '4.10.1'
TRIMESH_WHEEL_SHA256 = '4e81fae696683dfe912ef54ce124869487d35d267b87e10fe07fc05ab62aaadb'
MAX_CELLS_PER_AXIS = 256


def reference_config(cells=128):
    buildings = []
    for row in range(3):
        for column in range(4):
            buildings.append(dict(id=f'building_{row}_{column}', center_xy_m=[600 + column * 100, 600 + row * 100],
                size_xy_m=[20, 30], storeys=(5, 9, 15)[row], floor_height_m=3))
    return dict(schema_version=1, scene_id='terrain_10km_procedural_reference_v1',
                extent_m=[10000, 10000], cells_per_axis=cells, relief_m=200,
                origin=dict(latitude_deg=46.607213, longitude_deg=14.278461, elevation_m=446., heading_deg=0.),
                radio=dict(frequency_hz=2412000000., ground_material='medium_dry_ground',
                           building_material='concrete', thickness_m=.2),
                buildings=buildings,
                sample_sites_xy_m={'gcs': [-2000, -2000], **{f'uav{i}': [-1800 + (i - 1) * 30, -1800] for i in range(1, 6)}})


def _fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError(f'{label} requires exactly {sorted(names)}')


def _finite(value):
    return isinstance(value, numbers.Real) and not isinstance(value, (bool, np.bool_)) and math.isfinite(value)


def validate(config):
    _fields(config, {'schema_version', 'scene_id', 'extent_m', 'cells_per_axis', 'relief_m',
                     'origin', 'radio', 'buildings', 'sample_sites_xy_m'}, 'terrain config')
    if type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('terrain schema_version must be 1')
    if not isinstance(config['scene_id'], str) or not config['scene_id']:
        raise ValueError('scene_id must be a nonempty string')
    extent = config['extent_m']
    if not isinstance(extent, list) or len(extent) != 2 or any(not _finite(v) or v <= 0 for v in extent):
        raise ValueError('extent_m requires two finite positive lengths')
    cells = config['cells_per_axis']
    if type(cells) is not int or not 2 <= cells <= MAX_CELLS_PER_AXIS:
        raise ValueError(f'cells_per_axis must be 2..{MAX_CELLS_PER_AXIS}; this is a preparation limit, not an RT performance guarantee')
    if not _finite(config['relief_m']) or not 0 <= config['relief_m'] <= 200:
        raise ValueError('reference relief_m must be 0..200 m')
    origin = config['origin']
    _fields(origin, {'latitude_deg', 'longitude_deg', 'elevation_m', 'heading_deg'}, 'origin')
    if (any(not _finite(v) for v in origin.values()) or not -90 < origin['latitude_deg'] < 90
            or not -180 <= origin['longitude_deg'] <= 180 or origin['heading_deg'] != 0):
        raise ValueError('origin requires valid geodetic coordinates and ENU heading_deg=0')
    radio = config['radio']
    _fields(radio, {'frequency_hz', 'ground_material', 'building_material', 'thickness_m'}, 'radio')
    if not _finite(radio['frequency_hz']) or radio['frequency_hz'] <= 0 or not _finite(radio['thickness_m']) or radio['thickness_m'] <= 0:
        raise ValueError('radio frequency/thickness must be finite and positive')
    for key in ('ground_material', 'building_material'):
        material = radio[key]
        if material not in ITU_FREQUENCIES_GHZ or not any(lo <= radio['frequency_hz'] / 1e9 <= hi for lo, hi in ITU_FREQUENCIES_GHZ[material]):
            raise ValueError(f'{key} is unsupported at the selected frequency')
    if not isinstance(config['buildings'], list) or not 1 <= len(config['buildings']) <= 256:
        raise ValueError('reference scene requires 1..256 buildings')
    ids = set()
    for building in config['buildings']:
        _fields(building, {'id', 'center_xy_m', 'size_xy_m', 'storeys', 'floor_height_m'}, 'building')
        name = building['id']
        if not isinstance(name, str) or not name or name in ids:
            raise ValueError('building IDs must be unique nonempty strings')
        ids.add(name)
        for key in ('center_xy_m', 'size_xy_m'):
            if not isinstance(building[key], list) or len(building[key]) != 2 or any(not _finite(v) for v in building[key]):
                raise ValueError(f'building {key} requires two finite values')
        if any(v <= 0 for v in building['size_xy_m']) or any(abs(c) + size / 2 >= length / 2 for c, size, length in zip(building['center_xy_m'], building['size_xy_m'], extent)):
            raise ValueError('building footprint must be positive and strictly inside the terrain')
        if type(building['storeys']) is not int or not 1 <= building['storeys'] <= 15 or not _finite(building['floor_height_m']) or building['floor_height_m'] <= 0:
            raise ValueError('building requires 1..15 storeys and a finite positive floor height')
    sites = config['sample_sites_xy_m']
    if not isinstance(sites, dict) or not sites:
        raise ValueError('sample_sites_xy_m must provide named ground sample positions')
    for name, point in sites.items():
        if (not isinstance(name, str) or not isinstance(point, list) or len(point) != 2
                or any(not _finite(v) or abs(v) > length / 2 for v, length in zip(point, extent))):
            raise ValueError('sample site must lie on the terrain with finite XY coordinates')


def terrain_grid(config):
    validate(config)
    cells = config['cells_per_axis']
    width, length = config['extent_m']
    x = np.linspace(-width / 2, width / 2, cells + 1)
    y = np.linspace(-length / 2, length / 2, cells + 1)
    xx, yy = np.meshgrid(x, y)
    # Deterministic broad hills; no external DEM, vegetation or field calibration.
    z = np.exp(-((xx / width - .23) ** 2 + (yy / length - .17) ** 2) / .025)
    z += .35 * np.exp(-((xx / width + .22) ** 2 + (yy / length + .13) ** 2) / .04)
    z += .08 * np.sin(xx / width * 4 * np.pi) * np.sin(yy / length * 3 * np.pi)
    z = (z - z.min()) / (z.max() - z.min()) * config['relief_m']
    return dict(x_m=x, y_m=y, z_m=z)


def terrain_height(grid, x, y):
    """Exact piecewise-linear interpolation of the exported cell diagonal a→d."""
    xs, ys, zs = grid['x_m'], grid['y_m'], grid['z_m']
    if not _finite(x) or not _finite(y) or not xs[0] <= x <= xs[-1] or not ys[0] <= y <= ys[-1]:
        raise ValueError('ground-height query is outside the shared terrain')
    i = min(len(xs) - 2, max(0, int(np.searchsorted(xs, x, side='right') - 1)))
    j = min(len(ys) - 2, max(0, int(np.searchsorted(ys, y, side='right') - 1)))
    u, v = (x - xs[i]) / (xs[i + 1] - xs[i]), (y - ys[j]) / (ys[j + 1] - ys[j])
    if v <= u:
        return float((1 - u) * zs[j, i] + (u - v) * zs[j, i + 1] + v * zs[j + 1, i + 1])
    return float((1 - v) * zs[j, i] + u * zs[j + 1, i + 1] + (v - u) * zs[j + 1, i])


def meshes(config):
    import trimesh
    if trimesh.__version__ != TRIMESH_VERSION:
        raise ValueError(f'preparation requires pinned trimesh=={TRIMESH_VERSION}')
    grid = terrain_grid(config)
    xx, yy = np.meshgrid(grid['x_m'], grid['y_m'])
    vertices = np.column_stack((xx.ravel(), yy.ravel(), grid['z_m'].ravel()))
    cells = config['cells_per_axis']
    a = (np.arange(cells)[:, None] * (cells + 1) + np.arange(cells)[None, :]).ravel()
    faces = np.concatenate((np.column_stack((a, a + 1, a + cells + 2)),
                            np.column_stack((a, a + cells + 2, a + cells + 1))))
    terrain = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    buildings, bounds = [], {}
    for building in config['buildings']:
        cx, cy = building['center_xy_m']
        sx, sy = building['size_xy_m']
        xs, ys = grid['x_m'], grid['y_m']
        # All vertices of cells intersecting the footprint bound the height of
        # their linear triangles conservatively, including diagonal crossings.
        i0, i1 = np.searchsorted(xs, [cx - sx / 2, cx + sx / 2], side='right') - 1
        j0, j1 = np.searchsorted(ys, [cy - sy / 2, cy + sy / 2], side='right') - 1
        support = grid['z_m'][j0:j1 + 2, i0:i1 + 2]
        base, datum = float(support.min()) - .5, float(support.max()) + .1
        roof = datum + building['storeys'] * building['floor_height_m']
        box_bounds = [[cx - sx / 2, cy - sy / 2, base], [cx + sx / 2, cy + sy / 2, roof]]
        buildings.append(trimesh.creation.box(bounds=box_bounds))
        bounds[building['id']] = dict(bounds_m=box_bounds, storeys=building['storeys'],
            floor_height_m=building['floor_height_m'], floor_datum_z_m=datum,
            geometry='solid reference cuboid; no interior floor/room geometry')
    return grid, terrain, trimesh.util.concatenate(buildings), bounds


def prepare(config, output):
    started = time.perf_counter()
    output = Path(output)
    if output.exists():
        raise FileExistsError(f'scene bundle exists: {output}')
    grid, terrain, buildings, building_bounds = meshes(config)
    output.mkdir(parents=True, exist_ok=False)
    import trimesh
    distribution = importlib.metadata.distribution('trimesh')
    license_file = next((distribution.locate_file(path) for path in distribution.files
                         if str(path).endswith('licenses/LICENSE.md')), None)
    if license_file is None:
        raise ValueError('pinned trimesh wheel license is missing')
    (output / 'licenses').mkdir()
    (output / 'licenses/trimesh-MIT.txt').write_bytes(Path(license_file).read_bytes())
    for name, mesh in [('terrain', terrain), ('buildings', buildings)]:
        # Gazebo's DART CustomMeshShape requires a normal for each loaded
        # vertex; an OBJ without normals is discarded before collision setup.
        mesh.export(output / (name + '.obj'), file_type='obj', include_normals=True,
                    include_color=False, include_texture=False, digits=9)
    np.savez_compressed(output / 'terrain-grid.npz', **grid)
    (output / 'source.json').write_text(json.dumps(config, indent=2, allow_nan=False) + '\n')
    sdf = ET.Element('sdf', version='1.9')
    world = ET.SubElement(sdf, 'world', name='map')
    physics = ET.SubElement(world, 'physics', name='reference', type='ignored')
    ET.SubElement(physics, 'max_step_size').text = '.001'
    ET.SubElement(physics, 'real_time_factor').text = '1.0'
    ET.SubElement(world, 'gravity').text = '0 0 -9.80665'
    ET.SubElement(world, 'plugin', filename='gz-sim-physics-system', name='gz::sim::systems::Physics')
    ET.SubElement(world, 'plugin', filename='gz-sim-scene-broadcaster-system', name='gz::sim::systems::SceneBroadcaster')
    spherical = ET.SubElement(world, 'spherical_coordinates')
    ET.SubElement(spherical, 'surface_model').text = 'EARTH_WGS84'
    ET.SubElement(spherical, 'world_frame_orientation').text = 'ENU'
    for key, value in config['origin'].items():
        tag = dict(latitude_deg='latitude_deg', longitude_deg='longitude_deg', elevation_m='elevation', heading_deg='heading_deg')[key]
        ET.SubElement(spherical, tag).text = str(value)
    rt = ET.Element('scene', version='2.1.0')
    for name in ('ground', 'building'):
        material = ET.SubElement(rt, 'bsdf', type='itu-radio-material', id='material-' + name)
        ET.SubElement(material, 'string', name='type', value=config['radio'][name + '_material'])
        ET.SubElement(material, 'float', name='thickness', value=str(config['radio']['thickness_m']))
    for name in ('terrain', 'buildings'):
        model = ET.SubElement(world, 'model', name=name)
        ET.SubElement(model, 'static').text = 'true'
        ET.SubElement(model, 'pose').text = '0 0 0 0 0 0'
        link = ET.SubElement(model, 'link', name='body')
        for kind in ('visual', 'collision'):
            geometry = ET.SubElement(ET.SubElement(link, kind, name=kind), 'geometry')
            mesh = ET.SubElement(geometry, 'mesh')
            ET.SubElement(mesh, 'uri').text = name + '.obj'
            ET.SubElement(mesh, 'scale').text = '1 1 1'
        shape = ET.SubElement(rt, 'shape', type='obj', id='mesh-' + name)
        ET.SubElement(shape, 'string', name='filename', value=name + '.obj')
        ET.SubElement(shape, 'ref', name='bsdf', id='material-' + ('ground' if name == 'terrain' else 'building'))
    for element, name in ((sdf, 'world.sdf'), (rt, 'scene.xml')):
        ET.indent(element)
        ET.ElementTree(element).write(output / name, encoding='utf-8', xml_declaration=True)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = dict(schema_version=1, scene_id=config['scene_id'], source='procedural uncalibrated reference; no customer/third-party map',
        coordinate_frame='ENU', units='m', transforms='identity in both consumers', origin=config['origin'],
        extent_m=config['extent_m'], relief_m=float(grid['z_m'].max() - grid['z_m'].min()),
        cells_per_axis=config['cells_per_axis'], terrain_vertices=len(terrain.vertices), terrain_triangles=len(terrain.faces),
        building_triangles=len(buildings.faces), building_bounds=building_bounds,
        materials=dict(config['radio'], calibration='uncalibrated ITU reference'),
        consumer_geometry={name: dict(gazebo_visual=name + '.obj', gazebo_collision=name + '.obj', sionna=name + '.obj')
                           for name in ('terrain', 'buildings')},
        ground_sites={name: dict(xy_m=point, ground_z_m=terrain_height(grid, *point)) for name, point in config['sample_sites_xy_m'].items()},
        producer_dependencies=dict(numpy=np.__version__, trimesh=trimesh.__version__),
        trimesh_dependency=dict(version=TRIMESH_VERSION, wheel_sha256=TRIMESH_WHEEL_SHA256, license='MIT',
            source='https://pypi.org/project/trimesh/4.10.1/'),
        files={path.relative_to(output).as_posix(): dict(sha256=digest(path), bytes=path.stat().st_size)
               for path in sorted(output.rglob('*')) if path.is_file()},
        preparation_wall_s=time.perf_counter() - started,
        qualification=dict(gazebo_runtime='not_performed', collision_runtime='not_performed', sionna_runtime='not_performed',
                           agl_flight_integration='not_implemented', rt_performance='not_evaluated'),
        runtime_resource_path='GZ_SIM_RESOURCE_PATH must include this bundle directory')
    (output / 'scene-manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--example', action='store_true')
    args = parser.parse_args(argv)
    if args.example:
        print(json.dumps(reference_config(), indent=2))
        return 0
    if args.output is None:
        parser.error('--output requires a new directory')
    config = json.loads(args.config.read_text()) if args.config else reference_config()
    print(json.dumps(prepare(config, args.output), indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError) as error:
        print(f'Terrain scene: {error}', file=sys.stderr)
        raise SystemExit(2)
