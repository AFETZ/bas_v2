import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np
import trimesh

from scripts.product.prepare_terrain_scene import (MAX_CELLS_PER_AXIS, TRIMESH_VERSION,
    meshes, prepare, reference_config, terrain_grid, terrain_height, validate)


class TerrainSceneTests(unittest.TestCase):
    def test_nominal_128_and_256_grids_have_10km_extent_and_200m_relief(self):
        for cells in (128, 256):
            with self.subTest(cells=cells):
                grid, terrain, buildings, bounds = meshes(reference_config(cells))
                np.testing.assert_allclose(terrain.bounds[:, :2], [[-5000, -5000], [5000, 5000]])
                self.assertAlmostEqual(float(terrain.bounds[1, 2] - terrain.bounds[0, 2]), 200.)
                self.assertEqual(len(terrain.vertices), (cells + 1) ** 2)
                self.assertEqual(len(terrain.faces), 2 * cells ** 2)
                self.assertTrue(np.all(terrain.face_normals[:, 2] > 0))
                self.assertEqual(max(value['storeys'] for value in bounds.values()), 15)
                self.assertEqual(len(buildings.faces), 12 * len(bounds))

    def test_ground_interpolation_matches_exported_triangle_planes(self):
        config = reference_config(16)
        grid, terrain, _, _ = meshes(config)
        generator = np.random.default_rng(11)
        points = []
        triangles = []
        # Verify barycentric reconstruction through trimesh independently of
        # the structured-grid lookup, including both cell diagonal halves.
        for face in generator.choice(len(terrain.faces), size=50, replace=False):
            triangle = terrain.vertices[terrain.faces[face]]
            weights = generator.random(3)
            weights /= weights.sum()
            point = weights @ triangle
            point[2] = terrain_height(grid, point[0], point[1])
            points.append(point)
            triangles.append(triangle)
        points, triangles = np.array(points), np.array(triangles)
        barycentric = trimesh.triangles.points_to_barycentric(triangles, points)
        reconstructed = trimesh.triangles.barycentric_to_points(triangles, barycentric)
        np.testing.assert_allclose(reconstructed, points, atol=1e-9)
        for j, y in enumerate(grid['y_m']):
            for i, x in enumerate(grid['x_m']):
                self.assertAlmostEqual(terrain_height(grid, x, y), grid['z_m'][j, i])

    def test_both_consumers_reference_exact_same_obj_and_identity_transform(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'bundle'
            config = reference_config(16)
            result = prepare(config, output)
            world = ET.parse(output / 'world.sdf').getroot().find('world')
            rt = ET.parse(output / 'scene.xml').getroot()
            for name in ('terrain', 'buildings'):
                model = world.find(f"model[@name='{name}']")
                self.assertEqual(model.findtext('pose'), '0 0 0 0 0 0')
                self.assertEqual([value.text for value in model.findall('.//mesh/uri')], [name + '.obj'] * 2)
                shape = rt.find(f"shape[@id='mesh-{name}']")
                self.assertEqual(shape.get('type'), 'obj')
                self.assertEqual(shape.find("string[@name='filename']").get('value'), name + '.obj')
                self.assertIsNone(shape.find('transform'))
                digest = hashlib.sha256((output / (name + '.obj')).read_bytes()).hexdigest()
                self.assertEqual(result['files'][name + '.obj']['sha256'], digest)
                lines = (output / (name + '.obj')).read_text().splitlines()
                vertices = [line for line in lines if line.startswith('v ')]
                normals = [line for line in lines if line.startswith('vn ')]
                self.assertEqual(len(normals), len(vertices), 'DART rejects submeshes without per-vertex normals')
                for line in lines:
                    if line.startswith('f '):
                        for corner in line.split()[1:]:
                            fields = corner.split('/')
                            self.assertEqual(len(fields), 3)
                            self.assertTrue(1 <= int(fields[2]) <= len(normals))
            loaded = trimesh.load(output / 'terrain.obj', process=False)
            self.assertEqual(len(loaded.faces), result['terrain_triangles'])
            with np.load(output / 'terrain-grid.npz') as grid:
                for name, site in result['ground_sites'].items():
                    self.assertAlmostEqual(site['ground_z_m'], terrain_height(grid, *config['sample_sites_xy_m'][name]))
            self.assertEqual(result['producer_dependencies']['trimesh'], TRIMESH_VERSION)
            self.assertIn('The MIT License', (output / 'licenses/trimesh-MIT.txt').read_text())
            self.assertEqual(result['qualification']['agl_flight_integration'], 'not_implemented')
            self.assertEqual(json.loads((output / 'source.json').read_text()), config)
            with self.assertRaises(FileExistsError):
                prepare(config, output)

    def test_unknown_bad_or_excessive_configuration_is_rejected(self):
        config = reference_config(16)
        cases = []
        for field, value in [('cells_per_axis', MAX_CELLS_PER_AXIS + 1), ('cells_per_axis', True),
                              ('relief_m', float('nan')), ('extent_m', [0, 10000]), ('unknown', 1)]:
            case = copy.deepcopy(config)
            case[field] = value
            cases.append(case)
        case = copy.deepcopy(config)
        case['buildings'][0]['storeys'] = 16
        cases.append(case)
        case = copy.deepcopy(config)
        case['sample_sites_xy_m']['outside'] = [5001, 0]
        cases.append(case)
        case = copy.deepcopy(config)
        case['origin']['heading_deg'] = 10
        cases.append(case)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate(case)
        grid = terrain_grid(config)
        with self.assertRaises(ValueError):
            terrain_height(grid, 5001, 0)

    def test_flat_reference_and_ground_queries_are_finite(self):
        config = reference_config(8)
        config['relief_m'] = 0
        grid = terrain_grid(config)
        self.assertEqual(terrain_height(grid, 0, 0), 0.)
        self.assertEqual(terrain_height(grid, 5000, -5000), 0.)
        for point in [(True, 0), (float('nan'), 0), (0, float('inf'))]:
            with self.assertRaises(ValueError):
                terrain_height(grid, *point)


if __name__ == '__main__':
    unittest.main()
