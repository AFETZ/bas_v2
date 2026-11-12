"""Check the paired inputs and continuous nominal route clearance, not flight success."""
import copy
import json
import math
from pathlib import Path
import unittest

import yaml

from scripts.product.prepare_terrain_scene import terrain_grid, terrain_height

ROOT = Path(__file__).resolve().parents[1]


def segment_breakpoints(start, end, grid):
    """All affine terrain breaks along a segment of the uniform triangle mesh.

    Altitude and terrain are linear between these points, so evaluating their
    endpoints gives the exact minimum clearance, rather than a sampled guess.
    """
    points = {0., 1.}
    for axis, key in ((0, 'x_m'), (1, 'y_m')):
        delta = end[axis] - start[axis]
        if delta:
            points.update((float(v) - start[axis]) / delta for v in grid[key]
                          if 0 < (float(v) - start[axis]) / delta < 1)
    xs, ys = grid['x_m'], grid['y_m']
    dx, dy = float(xs[1] - xs[0]), float(ys[1] - ys[0])
    # Exported cells use their lower-left -> upper-right diagonal.
    a = (start[1] - ys[0]) / dy - (start[0] - xs[0]) / dx
    b = (end[1] - ys[0]) / dy - (end[0] - xs[0]) / dx
    if a != b:
        points.update((k - a) / (b - a) for k in range(math.floor(min(a, b)), math.ceil(max(a, b)) + 1)
                      if 0 < (k - a) / (b - a) < 1)
    return sorted(points)


class RefinedCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads((ROOT / 'runtime-refined-baseline.json').read_text())
        cls.source = json.loads((ROOT / 'runtime-refined-source.json').read_text())
        cls.scene = json.loads((ROOT / cls.source['environment']['scene']).read_text())
        cls.geometry = json.loads((ROOT / 'network/config/terrain_10km_geometry.json').read_text())
        cls.grid = terrain_grid(cls.geometry)

    def test_paired_inputs_differ_only_in_source_activation(self):
        expected = copy.deepcopy(self.source)
        expected['interference']['sources'] = None
        self.assertEqual(self.baseline, expected)
        self.assertEqual(self.source['drone']['count'], 5)
        self.assertEqual(self.source['flows']['channels'], ['control', 'payload'])
        self.assertEqual(self.source['flows']['payload_load']['uav_ids'], list(range(1, 6)))
        self.assertEqual(self.source['flows']['payload_load']['useful_bitrate_bps'], 10000)
        radio = yaml.safe_load((ROOT / self.source['propagation']['radio_config']).read_text())
        self.assertEqual(radio['radio']['shared_spectrum_channels'], 1)
        self.assertEqual(self.source['propagation']['model'], 'sionna')
        self.assertEqual(self.geometry['extent_m'], [10000, 10000])
        self.assertEqual(self.geometry['relief_m'], 200)
        self.assertEqual(max(b['storeys'] for b in self.geometry['buildings']), 15)

    def test_source_leaves_baseline_and_recovery_windows_available(self):
        sources = yaml.safe_load((ROOT / self.source['interference']['sources']).read_text())
        self.assertEqual(sources['time_reference'], 'measurement_start')
        self.assertEqual(len(sources['sources']), 1)
        source = sources['sources'][0]
        self.assertEqual((source['start_s'], source['stop_s']), (20, 40))
        self.assertEqual(self.source['experiment']['duration_s'], 90)
        self.assertEqual(source['pattern'], 'tr38901')
        self.assertEqual(source['center_hz'], self.scene['radio']['frequency_hz'])
        self.assertGreater(source['bandwidth_hz'], 0)

    def test_entire_nominal_routes_clear_triangles_and_building_footprints(self):
        for name, route in self.scene['routes'].items():
            with self.subTest(uav=name):
                self.assertEqual(route[0], route[-1])
                self.assertEqual(route[0][:2], self.scene['spawns'][name][:2])
                self.assertGreaterEqual(sum(math.dist(a, b) for a, b in zip(route, route[1:])), 70)
                expected_agl = 30 + (int(name) - 1) * 5 + self.scene['mission']['model_rest_clearance_m']
                for start, end in zip(route, route[1:]):
                    for fraction in segment_breakpoints(start, end, self.grid):
                        point = [a + fraction * (b - a) for a, b in zip(start, end)]
                        clearance = point[2] - terrain_height(self.grid, *point[:2])
                        self.assertAlmostEqual(clearance, expected_agl, delta=.1)
                        self.assertGreaterEqual(clearance, 30)
                    # A disjoint segment bounding box is a sufficient continuous
                    # clearance proof for these local routes, not just endpoints.
                    for building in self.geometry['buildings']:
                        self.assertTrue(any(
                            max(start[axis], end[axis]) < center - width / 2 or
                            min(start[axis], end[axis]) > center + width / 2
                            for axis, (center, width) in enumerate(zip(
                                building['center_xy_m'], building['size_xy_m']))))

    def test_diagonal_crossing_is_included_when_endpoints_miss_terrain_peak(self):
        # Opposite triangle corners: the cell diagonal has a 10 m ridge.
        import numpy as np
        grid = dict(x_m=np.array([0., 1.]), y_m=np.array([0., 1.]),
                    z_m=np.array([[10., 0.], [0., 10.]]))
        start, end = (0., 1., 12.), (1., 0., 12.)
        self.assertEqual(segment_breakpoints(start, end, grid), [0., .5, 1.])
        self.assertEqual(12. - terrain_height(grid, .5, .5), 2.)


if __name__ == '__main__':
    unittest.main()
