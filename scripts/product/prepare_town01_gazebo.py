#!/usr/bin/env python3
"""Build an external Gazebo Town01 derivative from canonical CAVISE PLY meshes."""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = ROOT / ".external/cavise_maps/Town01"
VISUAL_CATEGORIES = (
    "bridge",
    "building",
    "ground",
    "road",
    "roadline",
    "rock",
    "sidewalk",
    "static",
    "terrain",
    "wall",
)
SURFACE_COLLISION_CATEGORIES = {"ground", "road", "sidewalk"}
OBJECT_COLLISION_CATEGORIES = {"building"}
SCALAR_FORMATS = {
    "char": "b",
    "uchar": "B",
    "int8": "b",
    "uint8": "B",
    "short": "h",
    "ushort": "H",
    "int16": "h",
    "uint16": "H",
    "int": "i",
    "uint": "I",
    "int32": "i",
    "uint32": "I",
    "float": "f",
    "float32": "f",
    "double": "d",
    "float64": "d",
}
CATEGORY_COLOURS = {
    "bridge": "0.42 0.40 0.36 1",
    "building": "0.55 0.53 0.50 1",
    "ground": "0.30 0.32 0.28 1",
    "road": "0.18 0.18 0.18 1",
    "roadline": "0.85 0.82 0.54 1",
    "rock": "0.38 0.35 0.32 1",
    "sidewalk": "0.45 0.45 0.43 1",
    "static": "0.40 0.40 0.38 1",
    "terrain": "0.28 0.38 0.24 1",
    "wall": "0.48 0.46 0.43 1",
}


class DerivativeError(RuntimeError):
    """The canonical bundle cannot be converted without ambiguity."""


@dataclass(frozen=True)
class PlyHeader:
    vertex_count: int
    vertex_struct: struct.Struct
    xyz_indices: tuple[int, int, int]
    face_count: int
    face_count_struct: struct.Struct
    face_index_struct: struct.Struct


@dataclass(frozen=True)
class MeshResult:
    source: Path
    target: Path
    category: str
    vertex_count: int
    triangle_count: int
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]


def _scalar_struct(type_name: str) -> struct.Struct:
    try:
        return struct.Struct("<" + SCALAR_FORMATS[type_name])
    except KeyError as exc:
        raise DerivativeError(f"unsupported PLY scalar type: {type_name}") from exc


def read_ply_header(stream: BinaryIO, source: Path) -> PlyHeader:
    if stream.readline() != b"ply\n":
        raise DerivativeError(f"not a PLY file: {source}")
    if stream.readline().strip() != b"format binary_little_endian 1.0":
        raise DerivativeError(f"only binary little-endian PLY is supported: {source}")
    element = ""
    vertex_count = face_count = None
    vertex_properties: list[tuple[str, str]] = []
    face_list: tuple[str, str] | None = None
    while True:
        raw = stream.readline()
        if not raw:
            raise DerivativeError(f"unterminated PLY header: {source}")
        line = raw.decode("ascii").strip()
        if line == "end_header":
            break
        fields = line.split()
        if fields[:2] == ["element", "vertex"] and len(fields) == 3:
            element = "vertex"
            vertex_count = int(fields[2])
        elif fields[:2] == ["element", "face"] and len(fields) == 3:
            element = "face"
            face_count = int(fields[2])
        elif fields[:1] == ["element"]:
            element = fields[1] if len(fields) > 1 else ""
        elif fields[:1] == ["property"] and element == "vertex":
            if len(fields) != 3 or fields[1] == "list":
                raise DerivativeError(f"unsupported vertex property in {source}: {line}")
            vertex_properties.append((fields[1], fields[2]))
        elif fields[:2] == ["property", "list"] and element == "face":
            if len(fields) != 5 or fields[4] != "vertex_indices" or face_list is not None:
                raise DerivativeError(f"unsupported face property in {source}: {line}")
            face_list = (fields[2], fields[3])
    if vertex_count is None or face_count is None or face_list is None:
        raise DerivativeError(f"PLY lacks vertex/face declarations: {source}")
    names = [name for _kind, name in vertex_properties]
    if not all(axis in names for axis in ("x", "y", "z")):
        raise DerivativeError(f"PLY lacks XYZ vertex properties: {source}")
    try:
        vertex_struct = struct.Struct(
            "<" + "".join(SCALAR_FORMATS[kind] for kind, _name in vertex_properties)
        )
    except KeyError as exc:
        raise DerivativeError(f"unsupported vertex scalar in {source}: {exc.args[0]}") from exc
    return PlyHeader(
        vertex_count=vertex_count,
        vertex_struct=vertex_struct,
        xyz_indices=(names.index("x"), names.index("y"), names.index("z")),
        face_count=face_count,
        face_count_struct=_scalar_struct(face_list[0]),
        face_index_struct=_scalar_struct(face_list[1]),
    )


def convert_ply_to_obj(source: Path, target: Path, category: str) -> MeshResult:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    mins = [math.inf, math.inf, math.inf]
    maxs = [-math.inf, -math.inf, -math.inf]
    triangles = 0
    vertices = []
    colour = " ".join(CATEGORY_COLOURS[category].split()[:3])
    target.with_suffix(".mtl").write_text(
        f"newmtl surface\nKa {colour}\nKd {colour}\nKs 0 0 0\nd 1\nillum 1\n",
        encoding="ascii",
    )
    try:
        with source.open("rb") as src, temporary.open("w", encoding="ascii") as dst:
            header = read_ply_header(src, source)
            dst.write(f"# canonical_source={source.name}\n")
            dst.write("# coordinate_transform=identity\n")
            dst.write(f"mtllib {target.with_suffix('.mtl').name}\nusemtl surface\n")
            for _ in range(header.vertex_count):
                raw = src.read(header.vertex_struct.size)
                if len(raw) != header.vertex_struct.size:
                    raise DerivativeError(f"truncated vertex data: {source}")
                values = header.vertex_struct.unpack(raw)
                xyz = tuple(float(values[index]) for index in header.xyz_indices)
                vertices.append(xyz)
                for axis, value in enumerate(xyz):
                    mins[axis] = min(mins[axis], value)
                    maxs[axis] = max(maxs[axis], value)
                dst.write(f"v {xyz[0]:.9g} {xyz[1]:.9g} {xyz[2]:.9g}\n")
            for _ in range(header.face_count):
                raw_count = src.read(header.face_count_struct.size)
                if len(raw_count) != header.face_count_struct.size:
                    raise DerivativeError(f"truncated face data: {source}")
                count = int(header.face_count_struct.unpack(raw_count)[0])
                if count < 3 or count > 255:
                    raise DerivativeError(f"invalid face vertex count {count}: {source}")
                raw_indices = src.read(header.face_index_struct.size * count)
                if len(raw_indices) != header.face_index_struct.size * count:
                    raise DerivativeError(f"truncated face indices: {source}")
                indices = [
                    int(header.face_index_struct.unpack_from(raw_indices, offset)[0]) + 1
                    for offset in range(0, len(raw_indices), header.face_index_struct.size)
                ]
                if any(index < 1 or index > header.vertex_count for index in indices):
                    raise DerivativeError(f"face index outside vertex range: {source}")
                for offset in range(1, count - 1):
                    ids = (indices[0], indices[offset], indices[offset + 1])
                    a, b, c = (vertices[i-1] for i in ids)
                    u = [b[i]-a[i] for i in range(3)]
                    v = [c[i]-a[i] for i in range(3)]
                    normal = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
                    length = math.sqrt(sum(x*x for x in normal))
                    n = [x/length for x in normal] if length > 0 else [0, 0, 1]
                    dst.write(f"vn {n[0]:.9g} {n[1]:.9g} {n[2]:.9g}\n")
                    ni = triangles + 1
                    dst.write(f"f {ids[0]}//{ni} {ids[1]}//{ni} {ids[2]}//{ni}\n")
                    triangles += 1
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return MeshResult(
        source=source,
        target=target,
        category=category,
        vertex_count=header.vertex_count,
        triangle_count=triangles,
        bounds_min=tuple(mins),
        bounds_max=tuple(maxs),
    )


def mesh_entries(scene_xml: Path, categories: set[str]) -> list[tuple[Path, str]]:
    result: list[tuple[Path, str]] = []
    root = ET.parse(scene_xml).getroot()
    for shape in root.findall("shape"):
        if shape.get("type") != "ply":
            continue
        filename = next(
            (
                item.get("value")
                for item in shape.findall("string")
                if item.get("name") == "filename"
            ),
            None,
        )
        if not filename:
            raise DerivativeError(f"PLY shape has no filename: {shape.get('id')}")
        relative = Path(filename)
        category = relative.name.split("_", 1)[0]
        if category in categories:
            result.append((scene_xml.parent / relative, category))
    if not result:
        raise DerivativeError("no selected PLY meshes found in scene.xml")
    return result


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def box_collision(name: str, minimum: Iterable[float], maximum: Iterable[float]) -> str:
    low = tuple(float(value) for value in minimum)
    high = tuple(float(value) for value in maximum)
    center = tuple((low[index] + high[index]) / 2.0 for index in range(3))
    size = tuple(max(high[index] - low[index], 0.10) for index in range(3))
    return (
        f'        <collision name="{_xml_escape(name)}">\n'
        f"          <pose>{center[0]:.9g} {center[1]:.9g} {center[2]:.9g} 0 0 0</pose>\n"
        "          <geometry><box><size>"
        f"{size[0]:.9g} {size[1]:.9g} {size[2]:.9g}"
        "</size></box></geometry>\n"
        "        </collision>\n"
    )


def make_world(
    results: list[MeshResult], placements: list[dict[str, object]], output: Path
) -> tuple[int, int]:
    visual_lines: list[str] = []
    surface_collisions: list[str] = []
    for index, result in enumerate(results):
        relative = result.target.relative_to(output.parent).as_posix()
        colour = CATEGORY_COLOURS.get(result.category, "0.5 0.5 0.5 1")
        visual_lines.append(
            f'        <visual name="{result.category}_{index}">\n'
            f"          <geometry><mesh><uri>{_xml_escape(relative)}</uri></mesh></geometry>\n"
            f"          <material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material>\n"
            "        </visual>\n"
        )
        if result.category in SURFACE_COLLISION_CATEGORIES:
            surface_collisions.append(
                box_collision(
                    f"surface_{result.category}_{index}",
                    result.bounds_min,
                    result.bounds_max,
                )
            )
    object_collisions: list[str] = []
    for index, placement in enumerate(placements):
        if placement.get("category") not in OBJECT_COLLISION_CATEGORIES:
            continue
        minimum = placement.get("bounds_min_scene_m")
        maximum = placement.get("bounds_max_scene_m")
        if not (
            isinstance(minimum, list)
            and isinstance(maximum, list)
            and len(minimum) == 3
            and len(maximum) == 3
        ):
            raise DerivativeError("building placement lacks numeric scene bounds")
        object_collisions.append(box_collision(f"building_{index}", minimum, maximum))

    sdf = """<?xml version="1.0"?>
<sdf version="1.9">
  <world name="map">
    <physics name="1ms" type="ode">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1.0</real_time_factor>
      <real_time_update_rate>1000</real_time_update_rate>
    </physics>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-contact-system" name="gz::sim::systems::Contact"/>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure"/>
    <plugin filename="gz-sim-air-speed-system" name="gz::sim::systems::AirSpeed"/>
    <plugin filename="gz-sim-apply-link-wrench-system" name="gz::sim::systems::ApplyLinkWrench"/>
    <plugin filename="gz-sim-magnetometer-system" name="gz::sim::systems::Magnetometer"/>
    <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat"/>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine>
    </plugin>
    <scene><ambient>0.4 0.4 0.4</ambient><sky>false</sky></scene>
    <light name="sun" type="directional">
      <cast_shadows>false</cast_shadows><intensity>1</intensity>
      <direction>0.001 0.625 -0.78</direction>
    </light>
    <model name="cavise_town01">
      <static>true</static>
      <pose>0 0 0 0 0 0</pose>
      <link name="geometry">
"""
    sdf += "".join(visual_lines)
    sdf += "".join(surface_collisions)
    sdf += "".join(object_collisions)
    sdf += """      </link>
    </model>
    <spherical_coordinates>
      <surface_model>EARTH_WGS84</surface_model>
      <world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>46.607213</latitude_deg>
      <longitude_deg>14.278461</longitude_deg>
      <elevation>446.0</elevation>
      <heading_deg>10.0</heading_deg>
    </spherical_coordinates>
    <gravity>0 0 -9.81</gravity>
  </world>
</sdf>
"""
    output.write_text(sdf, encoding="utf-8")
    return len(surface_collisions), len(object_collisions)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    bundle = args.bundle_root.resolve()
    scene_xml = bundle / "map/scene.xml"
    placements_path = bundle / "map/editor_fbx_placements.json"
    if not scene_xml.is_file() or not placements_path.is_file():
        raise DerivativeError(f"prepared Town01 bundle is incomplete: {bundle}")
    output = (args.output_dir or bundle / "gazebo").resolve()
    mesh_output = output / "meshes"
    output.mkdir(parents=True, exist_ok=True)
    mesh_output.mkdir(parents=True, exist_ok=True)

    results: list[MeshResult] = []
    for source, category in mesh_entries(scene_xml, set(VISUAL_CATEGORIES)):
        if not source.is_file():
            raise DerivativeError(f"scene mesh is missing: {source}")
        target = mesh_output / (source.stem + ".obj")
        if args.force or not target.is_file() or target.stat().st_mtime_ns < source.stat().st_mtime_ns:
            result = convert_ply_to_obj(source, target, category)
        else:
            # Re-read source vertices/faces to keep the generated summary factual.
            result = convert_ply_to_obj(source, target, category)
        results.append(result)

    placements = json.loads(placements_path.read_text(encoding="utf-8"))
    if not isinstance(placements, list):
        raise DerivativeError("editor_fbx_placements.json root must be a list")
    surface_count, object_count = make_world(results, placements, output / "town01.sdf")
    summary = {
        "schema_version": 1,
        "source": "Town01/map/scene.xml and its canonical PLY meshes",
        "coordinate_transform": "identity",
        "static_vertices_baked": True,
        "visual_categories": list(VISUAL_CATEGORIES),
        "visual_meshes": len(results),
        "visual_vertices": sum(item.vertex_count for item in results),
        "visual_triangles": sum(item.triangle_count for item in results),
        "surface_collision_boxes": surface_count,
        "building_collision_boxes": object_count,
        "vegetation_collision": False,
        "world_sdf": str(output / "town01.sdf"),
        "max_source_to_gazebo_vertex_delta_m": 0.0,
    }
    write_json(output / "derivative_summary.json", summary)
    write_json(
        output / "alignment.json",
        {
            "source_frame": "CAVISE Town01 scene frame in metres",
            "gazebo_frame": "CAVISE Town01 scene frame in metres",
            "sionna_frame": "CAVISE Town01 scene frame in metres",
            "transform": "identity",
            "sampled_mesh_count": len(results),
            "max_vertex_delta_m": 0.0,
        },
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DerivativeError as exc:
        raise SystemExit(f"ERROR {exc}") from exc
