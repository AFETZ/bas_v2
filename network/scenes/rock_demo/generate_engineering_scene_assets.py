#!/usr/bin/env python3
"""Generate deterministic shared Gazebo/Sionna OBJ assets for rock_demo."""

from __future__ import annotations

import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORLD_DIR = ROOT / "src/multiagent_simulation/worlds/rock_demo"


def face_normal(
    vertices: list[tuple[float, float, float]], face: tuple[int, int, int]
) -> tuple[float, float, float]:
    """Return the deterministic unit normal for a one-based triangular face."""

    a, b, c = (vertices[index - 1] for index in face)
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    normal = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    length = math.sqrt(sum(value * value for value in normal))
    if length == 0.0:
        raise ValueError(f"degenerate face: {face}")
    return tuple(0.0 if abs(value / length) < 0.5e-12 else value / length for value in normal)


def write_normals_and_faces(
    stream,
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    *,
    vertex_offset: int = 0,
    normal_offset: int = 0,
) -> None:
    """Write one face normal per triangle for Gazebo/DART mesh collisions."""

    for face in faces:
        nx, ny, nz = face_normal(vertices, face)
        stream.write(f"vn {nx:.9f} {ny:.9f} {nz:.9f}\n")
    for index, (a, b, c) in enumerate(faces, start=1):
        normal = normal_offset + index
        stream.write(
            f"f {a + vertex_offset}//{normal} "
            f"{b + vertex_offset}//{normal} {c + vertex_offset}//{normal}\n"
        )


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
        write_normals_and_faces(stream, vertices, faces)


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
        vertex_offset = 0
        normal_offset = 0
        for name, cx, cy, sx, sy, sz in buildings:
            stream.write(f"o {name}\n")
            vertices = box_vertices(cx, cy, sx, sy, sz)
            for x, y, z in vertices:
                stream.write(f"v {x:.3f} {y:.3f} {z:.3f}\n")
            write_normals_and_faces(
                stream,
                vertices,
                local_faces,
                vertex_offset=vertex_offset,
                normal_offset=normal_offset,
            )
            vertex_offset += len(vertices)
            normal_offset += len(local_faces)


def write_blocker(path: Path) -> None:
    vertices = [
        (170.0, -140.0, 0.0),
        (230.0, -140.0, 0.0),
        (230.0, 140.0, 0.0),
        (170.0, 140.0, 0.0),
        (170.0, -140.0, 140.0),
        (230.0, -140.0, 140.0),
        (230.0, 140.0, 140.0),
        (170.0, 140.0, 140.0),
    ]
    faces = [
        (1, 2, 3), (1, 3, 4),
        (5, 8, 7), (5, 7, 6),
        (1, 5, 6), (1, 6, 2),
        (4, 3, 7), (4, 7, 8),
        (1, 4, 8), (1, 8, 5),
        (2, 6, 7), (2, 7, 3),
    ]
    with path.open("w", encoding="utf-8") as stream:
        stream.write("# Shared Gazebo/Sionna radio-blocker mesh.\n")
        stream.write("# Coordinates are ENU meters in the rock_demo world frame.\n")
        stream.write("o radio_blocker\n")
        for x, y, z in vertices:
            stream.write(f"v {x:.3f} {y:.3f} {z:.3f}\n")
        write_normals_and_faces(stream, vertices, faces)


def main() -> int:
    WORLD_DIR.mkdir(parents=True, exist_ok=True)
    write_terrain(WORLD_DIR / "engineering_terrain.obj")
    write_buildings(WORLD_DIR / "engineering_buildings.obj")
    write_blocker(WORLD_DIR / "radio_blocker.obj")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
