#!/usr/bin/env python3
"""Export checked static urban Gazebo boxes to pinned Sionna RT 1.2 scene XML.

Geometry is taken from the actual generated SDF and checked against its JSON
source. Materials are explicit ITU reference choices, not field calibration.
Mitsuba's native cube is a triangle mesh, so no mesh writer or RT solver is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

# Supported types/ranges from installed sionna-rt1.2.0 radio_materials/itu.py.
# The preparation step stays independent of GPU/Python3.10 native dependencies.
ITU_FREQUENCIES_GHZ = {
    'concrete': ((1., 100.),), 'brick': ((1., 40.),), 'plasterboard': ((1., 100.),),
    'wood': ((.001, 100.),), 'glass': ((.1, 100.), (220., 450.)),
    'ceiling_board': ((1., 100.), (220., 450.)), 'chipboard': ((1., 100.),),
    'plywood': ((1., 40.),), 'marble': ((1., 60.),), 'floorboard': ((50., 100.),),
    'metal': ((1., 100.),), 'very_dry_ground': ((1., 10.),),
    'medium_dry_ground': ((1., 10.),), 'wet_ground': ((1., 10.),),
}


def numbers(text, length):
    try:
        result = [float(item) for item in text.split()]
    except (AttributeError, ValueError) as error:
        raise ValueError('expected finite SDF coordinates') from error
    if len(result) != length or not all(math.isfinite(item) for item in result):
        raise ValueError(f'expected {length} finite SDF coordinates')
    return result


def static_boxes(world):
    """Only the exact axis-aligned static-box subset emitted by prepare_urban_gazebo."""
    try:
        root = ET.parse(world).getroot().find('world')
    except ET.ParseError as error:
        raise ValueError('invalid Gazebo world XML') from error
    if root is None or root.find('include') is not None:
        raise ValueError('expected generated urban world with explicit static models')
    boxes = {}
    for model in root.findall('model'):
        name = model.get('name')
        links = model.findall('link')
        if not name or name in boxes or model.findtext('static') != 'true' or len(links) != 1:
            raise ValueError('world requires uniquely named static single-link box models')
        collisions = links[0].findall('collision')
        if len(collisions) != 1 or collisions[0].find('geometry/box') is None:
            raise ValueError(f'{name}: only a single box collision is supported')
        poses = [numbers(item.findtext('pose', '0 0 0 0 0 0'), 6)
                 for item in (model, links[0], collisions[0])]
        if any(abs(angle) > 1e-12 for pose in poses for angle in pose[3:]):
            raise ValueError(f'{name}: rotated boxes require a separately qualified geometry adapter')
        centre = [sum(pose[axis] for pose in poses) for axis in range(3)]
        size = numbers(collisions[0].findtext('geometry/box/size'), 3)
        if any(value <= 0 for value in size):
            raise ValueError(f'{name}: box dimensions must be positive')
        boxes[name] = {'min': [c - s / 2 for c, s in zip(centre, size)],
                       'max': [c + s / 2 for c, s in zip(centre, size)]}
    return boxes


def prepare(config: Path, world: Path, output: Path, *, ground_material: str,
            building_material: str, thickness_m: float):
    data = json.loads(config.read_text(encoding='utf-8'))
    frequency = float(data['radio']['frequency_hz'])
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError('positive finite radio.frequency_hz is required')
    if type(thickness_m) not in (int, float) or not math.isfinite(thickness_m) or thickness_m <= 0:
        raise ValueError('explicit positive finite ITU slab thickness is required')
    for material in (ground_material, building_material):
        if not isinstance(material, str) or material not in ITU_FREQUENCIES_GHZ or not any(
                low <= frequency / 1e9 <= high for low, high in ITU_FREQUENCIES_GHZ[material]):
            raise ValueError(f'ITU material {material!r} is unsupported at {frequency} Hz in Sionna RT1.2')
    boxes = static_boxes(world)
    expected = {'urban_building_' + item['id']: item for item in data['buildings']}
    if len(expected) != len(data['buildings']) or set(boxes) != {'urban_ground', *expected}:
        raise ValueError('Gazebo collision model roster differs from the shared urban JSON')
    for name, item in expected.items():
        for bound in ('min', 'max'):
            values = item[bound]
            if (not isinstance(values, list) or len(values) != 3 or not all(
                    math.isclose(a, float(b), rel_tol=1e-12, abs_tol=1e-7)
                    for a, b in zip(boxes[name][bound], values))):
                raise ValueError(f'{name}: Gazebo bounds disagree with shared urban JSON')
    if not math.isclose(boxes['urban_ground']['max'][2], float(data['frame']['ground_surface_z_m']), abs_tol=1e-7):
        raise ValueError('Gazebo ground disagrees with the shared coordinate frame')
    scene = ET.Element('scene', version='2.1.0')
    for name, material in [('ground', ground_material), ('building', building_material)]:
        bsdf = ET.SubElement(scene, 'bsdf', type='itu-radio-material', id='material-' + name)
        ET.SubElement(bsdf, 'string', name='type', value=material)
        ET.SubElement(bsdf, 'float', name='thickness', value=str(thickness_m))
    for name, bounds in boxes.items():
        shape = ET.SubElement(scene, 'shape', type='cube', id='mesh-' + name)
        transform = ET.SubElement(shape, 'transform', name='to_world')
        half = [(hi - lo) / 2 for lo, hi in zip(bounds['min'], bounds['max'])]
        centre = [(hi + lo) / 2 for lo, hi in zip(bounds['min'], bounds['max'])]
        ET.SubElement(transform, 'scale', **dict(zip('xyz', map(str, half))))
        ET.SubElement(transform, 'translate', **dict(zip('xyz', map(str, centre))))
        material = 'ground' if name == 'urban_ground' else 'building'
        ET.SubElement(shape, 'ref', id='material-' + material, name='bsdf')
    output.mkdir(parents=True, exist_ok=True)
    ET.indent(scene)
    scene_file = output / 'scene.xml'
    ET.ElementTree(scene).write(scene_file, encoding='utf-8', xml_declaration=True)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = dict(schema_version=1, coordinate_frame='ENU', units='m',
                    source_json_sha256=digest(config), source_world_sha256=digest(world),
                    scene_sha256=digest(scene_file), frequency_hz=frequency, boxes=boxes,
                    materials=dict(ground=ground_material, buildings=building_material, thickness_m=thickness_m,
                                   status='uncalibrated_itu_reference'),
                    target_api=dict(sionna_rt='1.2.0', mitsuba='3.7.1'),
                    scope='static ground and buildings; no UAV body geometry or measured radio outcomes')
    (output / 'scene-manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return scene_file


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--world', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ground-material', required=True, choices=ITU_FREQUENCIES_GHZ)
    parser.add_argument('--building-material', required=True, choices=ITU_FREQUENCIES_GHZ)
    parser.add_argument('--thickness-m', type=float, required=True)
    args = parser.parse_args()
    try:
        print(prepare(args.config, args.world, args.output, ground_material=args.ground_material,
                      building_material=args.building_material, thickness_m=args.thickness_m))
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))
