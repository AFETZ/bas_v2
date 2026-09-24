#!/usr/bin/env python3
"""Focused alignment checks for the deterministic rugged rock-demo map."""

from __future__ import annotations

import math
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "src/multiagent_simulation/worlds/rock_demo"
WORLD_PATH = WORLD_DIR / "rock_demo.sdf"
SIONNA_PATH = WORLD_DIR / "sionna_scene.xml"
SCENARIO_PATH = ROOT / "network/config/scenario_rock_demo.yaml"

SHARED_MESHES = {
    "engineering_terrain_visual": "engineering_terrain.obj",
    "engineering_buildings_visual": "engineering_buildings.obj",
    "radio_blocker": "radio_blocker.obj",
}


def read_obj(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "v":
            vertices.append(tuple(float(value) for value in fields[1:4]))
        elif fields[0] == "f":
            indices = tuple(int(value.split("/", 1)[0]) - 1 for value in fields[1:4])
            if len(indices) != 3:
                raise AssertionError(f"non-triangular face in {path}: {line}")
            faces.append(indices)
    return vertices, faces


def terrain_height_at(x: float, y: float) -> float:
    vertices, faces = read_obj(WORLD_DIR / "engineering_terrain.obj")
    for face in faces:
        a, b, c = (vertices[index] for index in face)
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if math.isclose(denominator, 0.0):
            continue
        wa = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
        wb = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
        wc = 1.0 - wa - wb
        if min(wa, wb, wc) >= -1e-9:
            return wa * a[2] + wb * b[2] + wc * c[2]
    raise AssertionError(f"point ({x}, {y}) lies outside the terrain mesh")


def read_obj_with_normals(
    path: Path,
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[tuple[int, int], tuple[int, int], tuple[int, int]]],
]:
    vertices: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    faces: list[tuple[tuple[int, int], tuple[int, int], tuple[int, int]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "v":
            vertices.append(tuple(float(value) for value in fields[1:4]))
        elif fields[0] == "vn":
            normals.append(tuple(float(value) for value in fields[1:4]))
        elif fields[0] == "f":
            face = []
            for token in fields[1:4]:
                indices = token.split("/")
                if len(indices) != 3 or not indices[2]:
                    raise AssertionError(f"face lacks an explicit normal in {path}: {line}")
                face.append((int(indices[0]) - 1, int(indices[2]) - 1))
            faces.append(tuple(face))
    return vertices, normals, faces


class RockDemoGeometryTests(unittest.TestCase):
    def test_world_selects_a_mesh_capable_physics_engine(self) -> None:
        world = ET.parse(WORLD_PATH).getroot().find("world")
        self.assertIsNotNone(world)
        self.assertEqual(world.find("./physics").attrib["type"], "bullet")
        physics_system = world.find(
            "./plugin[@name='gz::sim::systems::Physics']"
        )
        self.assertIsNotNone(physics_system)
        self.assertEqual(
            physics_system.findtext("./engine/filename"),
            "gz-physics-bullet-featherstone-plugin",
        )

    def test_every_collision_triangle_has_a_matching_unit_normal(self) -> None:
        for mesh_name in SHARED_MESHES.values():
            with self.subTest(mesh=mesh_name):
                vertices, normals, faces = read_obj_with_normals(WORLD_DIR / mesh_name)
                self.assertEqual(len(normals), len(faces))
                for face_index, face in enumerate(faces):
                    normal_indices = {normal_index for _vertex_index, normal_index in face}
                    self.assertEqual(normal_indices, {face_index})
                    normal = normals[face_index]
                    self.assertAlmostEqual(math.sqrt(sum(value * value for value in normal)), 1.0, places=8)
                    a, b, c = (vertices[vertex_index] for vertex_index, _normal in face)
                    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
                    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
                    cross = (
                        ab[1] * ac[2] - ab[2] * ac[1],
                        ab[2] * ac[0] - ab[0] * ac[2],
                        ab[0] * ac[1] - ab[1] * ac[0],
                    )
                    cross_length = math.sqrt(sum(value * value for value in cross))
                    dot = sum(normal[index] * cross[index] / cross_length for index in range(3))
                    self.assertAlmostEqual(dot, 1.0, places=8)

    def test_gazebo_collision_and_sionna_use_identical_mesh_files(self) -> None:
        world = ET.parse(WORLD_PATH).getroot().find("world")
        self.assertIsNotNone(world)
        self.assertEqual(world.findall(".//collision/geometry/plane"), [])
        self.assertEqual(world.findall(".//collision/geometry/box"), [])

        sionna = ET.parse(SIONNA_PATH).getroot()
        sionna_meshes = {
            entry.attrib["value"]
            for entry in sionna.findall("./shape/string[@name='filename']")
        }
        self.assertEqual(sionna_meshes, set(SHARED_MESHES.values()))

        for model_name, mesh_name in SHARED_MESHES.items():
            with self.subTest(model=model_name):
                model = world.find(f"./model[@name='{model_name}']")
                self.assertIsNotNone(model)
                self.assertEqual(model.findtext("static"), "true")
                self.assertEqual(model.findtext("pose", default="0 0 0 0 0 0"), "0 0 0 0 0 0")
                collision_uri = model.findtext("./link/collision/geometry/mesh/uri")
                visual_uri = model.findtext("./link/visual/geometry/mesh/uri")
                self.assertEqual(collision_uri, mesh_name)
                self.assertEqual(visual_uri, mesh_name)
                self.assertEqual((WORLD_DIR / collision_uri).resolve(), (SIONNA_PATH.parent / mesh_name).resolve())
                self.assertTrue((WORLD_DIR / mesh_name).is_file())

    def test_scenario_metadata_matches_checked_in_terrain_mesh(self) -> None:
        scenario = yaml.safe_load(SCENARIO_PATH.read_text(encoding="utf-8"))
        map_config = scenario["scenario"]["map"]
        vertices, faces = read_obj(WORLD_DIR / "engineering_terrain.obj")
        xs, ys, zs = zip(*vertices)

        self.assertEqual(map_config["classification"], "deterministic_engineering_map")
        self.assertFalse(map_config["customer_map_eligible"])
        self.assertEqual(map_config["coordinate_frame"], "ENU_m")
        self.assertEqual(map_config["bounds_m"], {"xmin": min(xs), "ymin": min(ys), "xmax": max(xs), "ymax": max(ys)})
        self.assertEqual(map_config["size_m"], [max(xs) - min(xs), max(ys) - min(ys)])
        self.assertEqual(map_config["terrain_elevation_range_m"], [min(zs), max(zs)])
        self.assertAlmostEqual(map_config["terrain_height_variation_m"], max(zs) - min(zs), places=6)
        self.assertEqual(len(vertices), 1089)
        self.assertEqual(len(faces), 2048)

    def test_all_uavs_spawn_two_metres_above_the_exact_collision_mesh(self) -> None:
        scenario = yaml.safe_load(SCENARIO_PATH.read_text(encoding="utf-8"))
        for robot in scenario["robots"]:
            x, y, z = (float(value) for value in robot["position"][:3])
            clearance = z - terrain_height_at(x, y)
            with self.subTest(robot=robot["name"]):
                self.assertAlmostEqual(clearance, 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
