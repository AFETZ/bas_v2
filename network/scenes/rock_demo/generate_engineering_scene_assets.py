#!/usr/bin/env python3
"""Generate deterministic shared Gazebo/Sionna OBJ assets for rock_demo."""

from __future__ import annotations

import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORLD_DIR = ROOT / "src/multiagent_simulation/worlds/rock_demo"


def terrain_height(x: float, y: float) -> float:
    ridge = 95.0 * math.exp(-(((x - 1800.0) / 1700.0) ** 2 + ((y + 1600.0) / 1300.0) ** 2))
    hill = 70.0 * math.exp(-(((x + 2100.0) / 1400.0) ** 2 + ((y - 1700.0) / 1200.0) ** 2))
    undulation = 18.0 * math.sin(x / 1400.0) * math.cos(y / 1300.0)
    local = 24.0 * math.exp(-(((x - 350.0) / 520.0) ** 2 + (y / 900.0) ** 2))
    return max(0.0, ridge + hill + undulation + local)


def write_terrain(path: Path, n: int = 33, extent_m: float = 5000.0) -> None:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    step = (2.0 * extent_m) / float(n - 1)
    for iy in range(n):
        y = -extent_m + step * iy
        for ix in range(n):
            x = -extent_m + step * ix
            vertices.append((x, y, terrain_height(x, y)))
    for iy in range(n - 1):
        for ix in range(n - 1):
            v0 = iy * n + ix + 1
            v1 = v0 + 1
            v2 = v0 + n
            v3 = v2 + 1
            faces.append((v0, v1, v3))
            faces.append((v0, v3, v2))
    with path.open("w", encoding="utf-8") as stream:
        stream.write("# Deterministic 10 km x 10 km engineering terrain, ENU meters.\n")
        stream.write("o engineering_terrain\n")
        for x, y, z in vertices:
            stream.write(f"v {x:.3f} {y:.3f} {z:.3f}\n")
        for a, b, c in faces:
            stream.write(f"f {a} {b} {c}\n")


def box_vertices(cx: float, cy: float, sx: float, sy: float, sz: float) -> list[tuple[float, float, float]]:
    z0 = terrain_height(cx, cy)
    x0, x1 = cx - sx / 2.0, cx + sx / 2.0
    y0, y1 = cy - sy / 2.0, cy + sy / 2.0
    return [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z0 + sz),
        (x1, y0, z0 + sz),
        (x1, y1, z0 + sz),
        (x0, y1, z0 + sz),
    ]


def write_buildings(path: Path) -> None:
    buildings = [
        ("settlement_a_warehouse", 980.0, 720.0, 160.0, 80.0, 34.0),
        ("settlement_a_tower", 1220.0, 840.0, 70.0, 70.0, 62.0),
        ("settlement_a_row_1", 1420.0, 650.0, 110.0, 90.0, 44.0),
        ("settlement_b_block_1", -1580.0, -820.0, 130.0, 110.0, 48.0),
        ("settlement_b_block_2", -1360.0, -980.0, 90.0, 120.0, 58.0),
        ("settlement_b_mast_house", -1100.0, -760.0, 80.0, 80.0, 72.0),
    ]
    local_faces = [
        (1, 2, 3), (1, 3, 4),
        (5, 8, 7), (5, 7, 6),
        (1, 5, 6), (1, 6, 2),
        (4, 3, 7), (4, 7, 8),
        (1, 4, 8), (1, 8, 5),
        (2, 6, 7), (2, 7, 3),
    ]
    with path.open("w", encoding="utf-8") as stream:
        stream.write("# Deterministic engineering settlement blocks, ENU meters.\n")
        offset = 0
        for name, cx, cy, sx, sy, sz in buildings:
            stream.write(f"o {name}\n")
            for x, y, z in box_vertices(cx, cy, sx, sy, sz):
                stream.write(f"v {x:.3f} {y:.3f} {z:.3f}\n")
            for face in local_faces:
                a, b, c = (idx + offset for idx in face)
                stream.write(f"f {a} {b} {c}\n")
            offset += 8


def main() -> int:
    WORLD_DIR.mkdir(parents=True, exist_ok=True)
    write_terrain(WORLD_DIR / "engineering_terrain.obj")
    write_buildings(WORLD_DIR / "engineering_buildings.obj")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
