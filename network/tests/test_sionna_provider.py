#!/usr/bin/env python3
"""Focused correctness tests for the production Sionna provider."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider import provider  # noqa: E402


class SionnaPathEvidenceTests(unittest.TestCase):
    def test_empty_sionna_path_axes_are_blocked_no_path(self) -> None:
        """A zero-depth Sionna tensor is valid no-path evidence, not an error."""

        instance = object.__new__(provider.SionnaRadioProvider)
        instance._np = numpy
        evidence = instance._path_evidence(
            numpy.empty((1, 1, 0), dtype=bool),
            numpy.empty((1, 1, 0), dtype=float),
            numpy.empty((0, 1, 1, 0), dtype=int),
            0,
            0,
        )

        self.assertEqual(evidence.path_count, 0)
        self.assertEqual(evidence.geometry_state, "blocked_no_path")
        self.assertEqual(evidence.propagation_delay_ns, 0.0)
        self.assertEqual(evidence.path_type_counts, provider.empty_path_type_counts())


class SionnaSurfaceEpsilonTests(unittest.TestCase):
    def test_surface_epsilon_offsets_only_the_solver_position(self) -> None:
        raw_position = [-7000.0, -2500.0, 44.0]
        epsilon_m = provider.SionnaRadioProvider._sionna_surface_epsilon_m(
            {"surface_epsilon_m": 0.05}
        )

        solver_position = provider.SionnaRadioProvider._sionna_ray_origin_position(
            raw_position, epsilon_m
        )

        self.assertEqual(raw_position, [-7000.0, -2500.0, 44.0])
        self.assertEqual(solver_position, [-7000.0, -2500.0, 44.05])

    def test_surface_epsilon_rejects_nonphysical_values(self) -> None:
        for value in (True, -0.001, 0.051, float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(provider.ProviderError):
                    provider.SionnaRadioProvider._sionna_surface_epsilon_m(
                        {"surface_epsilon_m": value}
                    )


if __name__ == "__main__":
    unittest.main()
