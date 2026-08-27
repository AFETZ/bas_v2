#!/usr/bin/env python3
"""Inspect a CAVISE Sionna ZIP without extracting its large assets.

Only the ZIP central directory and explicitly allow-listed metadata members are
read. Mesh (PLY) and Blender payloads are never opened by this program.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


ARCHIVE_NAME_RE = re.compile(
    r"^CAVISE_SIONNA_(Town\d+)_EditorLOD0_Full_Official_\d{8}\.zip$",
    re.IGNORECASE,
)
SMALL_MEMBER_LIMITS = {
    "README.md": 2 * 1024 * 1024,
    "map/editor_fbx_scene.json": 16 * 1024 * 1024,
    "map/transforms.xml": 4 * 1024 * 1024,
    "map/cameras.xml": 4 * 1024 * 1024,
    "SHA256SUMS": 4 * 1024 * 1024,
}
PLACEMENTS_FRAGMENT_LIMIT = 256 * 1024


def normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def walk_mapping(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield normalized_key(key), child
            yield from walk_mapping(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_mapping(child)


def first_field(value: Any, *aliases: str) -> Any:
    wanted = {normalized_key(alias) for alias in aliases}
    for key, child in walk_mapping(value):
        if key in wanted:
            return child
    return None


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def integer_count(value: Any) -> int | None:
    number = finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "yes", "1", "on", "baked"}:
            return True
        if lowered in {"false", "no", "0", "off", "not_baked", "unbaked"}:
            return False
    return None


def numeric_vector(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        indexed = {normalized_key(key): child for key, child in value.items()}
        raw_values = []
        for axis in ("x", "y", "z"):
            raw_values.append(
                next((indexed[key] for key in (axis, f"{axis}m") if key in indexed), None)
            )
        values = [finite_number(item) for item in raw_values]
        if all(item is not None for item in values):
            return list(values)  # type: ignore[arg-type]
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        values = [finite_number(item) for item in value[:3]]
        return None if any(item is None for item in values) else list(values)  # type: ignore[arg-type]
    return None


def numeric_range(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        indexed = {normalized_key(key): child for key, child in value.items()}
        lower = next(
            (
                finite_number(indexed[key])
                for key in ("min", "minm", "minimum", "lower")
                if key in indexed
            ),
            None,
        )
        upper = next(
            (
                finite_number(indexed[key])
                for key in ("max", "maxm", "maximum", "upper")
                if key in indexed
            ),
            None,
        )
        if lower is not None and upper is not None:
            return [lower, upper]
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        lower, upper = finite_number(value[0]), finite_number(value[1])
        if lower is not None and upper is not None:
            return [lower, upper]
    return None


def normalize_bounds(value: Any, depth: int = 0) -> dict[str, list[float]] | None:
    """Accept common min/max and per-axis bounds encodings."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        indexed = {normalized_key(key): child for key, child in value.items()}
        axes: dict[str, list[float]] = {}
        for axis in ("x", "y", "z"):
            for candidate in (axis, f"{axis}m", f"{axis}range", f"{axis}bounds"):
                if candidate in indexed:
                    axis_range = numeric_range(indexed[candidate])
                    if axis_range is not None:
                        axes[axis] = axis_range
                        break
        if "x" in axes and "y" in axes:
            return axes

        scalar_axes: dict[str, list[float]] = {}
        for axis in ("x", "y", "z"):
            lower = next(
                (
                    finite_number(indexed[key])
                    for key in (
                        f"{axis}min",
                        f"{axis}minm",
                        f"min{axis}",
                        f"min{axis}m",
                        f"{axis}minimum",
                    )
                    if key in indexed
                ),
                None,
            )
            upper = next(
                (
                    finite_number(indexed[key])
                    for key in (
                        f"{axis}max",
                        f"{axis}maxm",
                        f"max{axis}",
                        f"max{axis}m",
                        f"{axis}maximum",
                    )
                    if key in indexed
                ),
                None,
            )
            if lower is not None and upper is not None:
                scalar_axes[axis] = [lower, upper]
        if "x" in scalar_axes and "y" in scalar_axes:
            return scalar_axes

        lower_raw = next(
            (indexed[key] for key in ("min", "minimum", "lower", "boundsmin") if key in indexed),
            None,
        )
        upper_raw = next(
            (indexed[key] for key in ("max", "maximum", "upper", "boundsmax") if key in indexed),
            None,
        )
        lower, upper = numeric_vector(lower_raw), numeric_vector(upper_raw)
        if lower is not None and upper is not None:
            return {
                "x": [lower[0], upper[0]],
                "y": [lower[1], upper[1]],
                "z": [lower[2], upper[2]],
            }

        for child in value.values():
            nested = normalize_bounds(child, depth + 1)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        if len(value) >= 6:
            numbers = [finite_number(item) for item in value[:6]]
            if all(item is not None for item in numbers):
                clean = list(numbers)  # type: ignore[arg-type]
                return {"x": clean[0:2], "y": clean[2:4], "z": clean[4:6]}
        if len(value) == 2:
            lower, upper = numeric_vector(value[0]), numeric_vector(value[1])
            if lower is not None and upper is not None:
                return {
                    "x": [lower[0], upper[0]],
                    "y": [lower[1], upper[1]],
                    "z": [lower[2], upper[2]],
                }
    return None


def text_count(text: str, label: str) -> int | None:
    match = re.search(
        rf"(?im)\b{label}\b\s*(?:count)?\s*[:=|]\s*([0-9][0-9_, ]*)",
        text,
    )
    if match is None:
        return None
    return int(re.sub(r"[^0-9]", "", match.group(1)))


def bounds_from_text(text: str) -> dict[str, list[float]] | None:
    anchor = re.search(r"(?i)retained[ _-]*bounds(?:[ _-]*scene)?(?:[ _-]*m)?", text)
    if anchor is None:
        return None
    fragment = text[anchor.start() : anchor.start() + 2000]
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    axes: dict[str, list[float]] = {}
    for axis in ("x", "y", "z"):
        match = re.search(
            rf"(?i)[\"']?{axis}(?:_m)?[\"']?\s*[:=]\s*[\[(]\s*"
            rf"({number})\s*[, ]+\s*({number})\s*[\])]",
            fragment,
        )
        if match:
            axes[axis] = [float(match.group(1)), float(match.group(2))]
    return axes if "x" in axes and "y" in axes else None


def all_versions(text: str, product: str) -> list[str]:
    matches = re.finditer(
        rf"(?im)\b{product}\b(?:\s+(?:RT|source|version))*\s*[:=v-]*\s*"
        r"([0-9]+(?:\.[0-9]+){1,3}(?:[-+._a-z0-9]*)?)",
        text,
    )
    return list(dict.fromkeys(match.group(1).rstrip(".,;:") for match in matches))


def first_version(text: str, product: str) -> str | None:
    versions = all_versions(text, product)
    return versions[0] if versions else None


def category_counts(value: Any) -> dict[str, int]:
    raw = first_field(
        value,
        "retained_category_counts",
        "placements_by_category",
        "category_counts",
        "categories_counts",
        "object_categories",
        "categories",
    )
    result: dict[str, int] = {}
    if isinstance(raw, dict):
        retained = next(
            (
                child
                for key, child in raw.items()
                if normalized_key(key) in {"retained", "retainedcounts"}
                and isinstance(child, dict)
            ),
            None,
        )
        if retained is not None:
            raw = retained
        for key, child in raw.items():
            count = integer_count(child)
            if count is None and isinstance(child, dict):
                count = integer_count(
                    first_field(child, "retained_count", "object_count", "count")
                )
            if count is not None:
                result[str(key)] = count
    elif isinstance(raw, list):
        for child in raw:
            if not isinstance(child, dict):
                continue
            name = first_field(child, "category", "name", "type", "label")
            count = integer_count(first_field(child, "retained_count", "object_count", "count"))
            if name is not None and count is not None:
                result[str(name)] = count
    return dict(sorted(result.items()))


def count_category(counts: dict[str, int], *tokens: str) -> int | None:
    normalized = {normalized_key(key): value for key, value in counts.items()}
    for token in tokens:
        if normalized_key(token) in normalized:
            return normalized[normalized_key(token)]
    matched = [
        value
        for key, value in normalized.items()
        if any(normalized_key(token) in key for token in tokens)
    ]
    return sum(matched) if matched else None


def xml_transform_summary(text: str, member_name: str) -> dict[str, Any] | None:
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    entries: list[dict[str, Any]] = []
    interesting = ("transform", "matrix", "frame", "axis", "offset", "origin", "scale")
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        attrs = {str(key): str(value) for key, value in element.attrib.items()}
        body = (element.text or "").strip()
        searchable = " ".join([tag, *attrs.keys()]).casefold()
        if element is root or any(token in searchable for token in interesting):
            entry: dict[str, Any] = {"tag": tag}
            if attrs:
                entry["attributes"] = attrs
            if body and len(body) <= 1000:
                entry["text"] = body
            entries.append(entry)
        if len(entries) >= 128:
            break
    return {
        "source_member": member_name,
        "root_tag": root.tag.rsplit("}", 1)[-1],
        "entries": entries,
    }


def find_sumo_offset(value: Any, transform_summary: dict[str, Any] | None) -> Any:
    result = first_field(
        value, "sumo_offset", "sumo_offset_m", "sumo_net_offset", "net_offset"
    )
    if result is not None:
        return result
    if transform_summary is None:
        return None
    for entry in transform_summary.get("entries", []):
        if "sumo" in json.dumps(entry, sort_keys=True).casefold() and "offset" in json.dumps(
            entry, sort_keys=True
        ).casefold():
            return entry
    return None


class MetadataZip:
    def __init__(self, archive: Path):
        self.archive = archive
        self.handle = zipfile.ZipFile(archive, "r", allowZip64=True)
        self.members = [info for info in self.handle.infolist() if not info.is_dir()]

    def __enter__(self) -> "MetadataZip":
        return self

    def __exit__(self, *_args: object) -> None:
        self.handle.close()

    def find(self, suffix: str) -> zipfile.ZipInfo | None:
        wanted = suffix.casefold().lstrip("/")
        matches = [
            info
            for info in self.members
            if info.filename.casefold().lstrip("/") == wanted
            or info.filename.casefold().lstrip("/").endswith("/" + wanted)
        ]
        if not matches:
            return None
        return min(matches, key=lambda info: (info.filename.count("/"), len(info.filename)))

    def read(self, info: zipfile.ZipInfo, limit: int) -> tuple[str, bool]:
        with self.handle.open(info, "r") as source:
            payload = source.read(limit + 1)
        truncated = len(payload) > limit
        return payload[:limit].decode("utf-8-sig", errors="replace"), truncated


def count_from_metadata(scene: Any, text: str, aliases: tuple[str, ...], label: str) -> int | None:
    count = integer_count(first_field(scene, *aliases))
    return count if count is not None else text_count(text, label)


def candidate_assessment(
    *,
    width_m: float | None,
    height_m: float | None,
    terrain_count: int | None,
    building_count: int | None,
    scene_xml_present: bool,
    blend_present: bool,
    transform_explicit: bool,
) -> tuple[bool, list[str]]:
    checks = [
        (width_m is not None and width_m >= 10_000, "retained width is at least 10000 m", "retained width is missing or below 10000 m"),
        (height_m is not None and height_m >= 10_000, "retained height is at least 10000 m", "retained height is missing or below 10000 m"),
        (terrain_count is not None and terrain_count > 0, "terrain is reported", "terrain count is missing or zero"),
        (building_count is not None and building_count > 0, "buildings are reported", "building count is missing or zero"),
        (scene_xml_present, "scene.xml is present", "scene.xml is absent"),
        (blend_present, "Blender artifact is present", "Blender artifact is absent"),
        (transform_explicit, "coordinate transform metadata is explicit", "coordinate transform metadata is missing"),
    ]
    accepted = all(result for result, _accept, _reject in checks)
    reasons = [
        ("accept: " + accept) if result else ("reject: " + reject)
        for result, accept, reject in checks
    ]
    if accepted:
        reasons.append(
            "pending ROI check: relief, settlement coverage, building coverage, and ROI z-span"
        )
    return accepted, reasons


def inspect_archive(archive: Path) -> dict[str, Any]:
    warnings: list[str] = []
    archive_match = ARCHIVE_NAME_RE.match(archive.name)
    town = archive_match.group(1) if archive_match else None

    with MetadataZip(archive) as source:
        by_suffix = {suffix: source.find(suffix) for suffix in SMALL_MEMBER_LIMITS}
        scene_xml = source.find("map/scene.xml")
        placements = source.find("map/editor_fbx_placements.json")
        blend_members = [info for info in source.members if info.filename.casefold().endswith(".blend")]
        ply_file_count = sum(info.filename.casefold().endswith(".ply") for info in source.members)

        texts: dict[str, str] = {}
        for suffix, limit in SMALL_MEMBER_LIMITS.items():
            info = by_suffix[suffix]
            if info is None:
                continue
            text, truncated = source.read(info, limit)
            texts[suffix] = text
            if truncated:
                warnings.append(
                    f"{info.filename} exceeded the {limit}-byte metadata read limit"
                )

        scene: Any = {}
        scene_text = texts.get("map/editor_fbx_scene.json", "")
        if scene_text:
            try:
                scene = json.loads(scene_text)
            except json.JSONDecodeError as exc:
                warnings.append(f"editor_fbx_scene.json could not be parsed: {exc}")

        placement_fragment = ""
        if placements is not None and not scene:
            placement_fragment, truncated = source.read(placements, PLACEMENTS_FRAGMENT_LIMIT)
            if truncated:
                warnings.append(
                    "editor_fbx_placements.json was sampled only; aggregate counts were not inferred"
                )

        all_text = "\n".join([*texts.values(), placement_fragment])
        if town is None:
            discovered_town = first_field(scene, "town", "map_name", "carla_map")
            if isinstance(discovered_town, str) and re.fullmatch(r"Town\d+", discovered_town):
                town = discovered_town

        raw_bounds = first_field(
            scene,
            "retained_bounds_scene_m",
            "retained_scene_bounds_m",
            "retained_bounds_m",
            "retained_bounds",
        )
        bounds = normalize_bounds(raw_bounds) or bounds_from_text(all_text)
        x_range = bounds.get("x") if bounds else None
        y_range = bounds.get("y") if bounds else None
        z_range = bounds.get("z") if bounds else None
        width_m = x_range[1] - x_range[0] if x_range else None
        height_m = y_range[1] - y_range[0] if y_range else None
        z_min_m = z_range[0] if z_range else None
        z_max_m = z_range[1] if z_range else None
        z_span_m = z_max_m - z_min_m if z_min_m is not None and z_max_m is not None else None

        counts = category_counts(scene)
        building_count = integer_count(first_field(scene, "building_count", "buildings_count"))
        terrain_count = integer_count(first_field(scene, "terrain_count", "terrains_count"))
        road_count = integer_count(first_field(scene, "road_count", "roads_count"))
        vegetation_count = integer_count(
            first_field(scene, "vegetation_count", "vegetations_count", "foliage_count")
        )
        building_count = building_count if building_count is not None else count_category(counts, "building")
        terrain_count = terrain_count if terrain_count is not None else count_category(counts, "terrain", "landscape")
        road_count = road_count if road_count is not None else count_category(counts, "road")
        vegetation_count = vegetation_count if vegetation_count is not None else count_category(
            counts, "vegetation", "foliage", "tree", "bush"
        )
        building_count = building_count if building_count is not None else text_count(all_text, "buildings?")
        terrain_count = terrain_count if terrain_count is not None else text_count(all_text, "terrains?")
        road_count = road_count if road_count is not None else text_count(all_text, "roads?")
        vegetation_count = vegetation_count if vegetation_count is not None else text_count(
            all_text, "vegetation"
        )
        if not counts:
            counts = {
                name: count
                for name, count in (
                    ("building", building_count),
                    ("terrain", terrain_count),
                    ("road", road_count),
                    ("vegetation", vegetation_count),
                )
                if count is not None
            }

        transform_info = first_field(
            scene,
            "coordinate_transform",
            "carla_to_scene_transform",
            "source_to_scene_transform",
            "coordinate_frames",
        )
        transform_xml = None
        transforms_info = by_suffix["map/transforms.xml"]
        if transforms_info is not None:
            transform_xml = xml_transform_summary(
                texts.get("map/transforms.xml", ""), transforms_info.filename
            )
            if transform_xml is None:
                warnings.append("transforms.xml could not be parsed as bounded XML metadata")
        coordinate_transform = transform_info if transform_info is not None else transform_xml
        transform_entries = transform_xml.get("entries", []) if transform_xml else []
        root_payload = (
            transform_entries[0].get("attributes") or transform_entries[0].get("text")
            if transform_entries
            else None
        )
        transform_explicit = transform_info is not None or bool(
            transform_entries[1:] or root_payload
        )

        source_carla_version = first_field(scene, "source_carla_version", "carla_version")
        if source_carla_version is None:
            source_carla_version = first_version(all_text, "CARLA")
        blender_version = first_field(scene, "blender_version", "source_blender_version")
        if blender_version is None:
            blender_version = first_version(all_text, "Blender")
        sionna_versions = first_field(
            scene, "sionna_versions", "sionna_version", "sionna_rt_version"
        )
        if sionna_versions is None:
            sionna_versions = all_versions(all_text, "Sionna")

        full_editor_world = boolean_value(first_field(scene, "full_editor_world"))
        if full_editor_world is None:
            match = re.search(
                r"(?im)\bfull[ _-]*editor[ _-]*world\b\s*[:=|]\s*(true|false|yes|no|1|0)",
                all_text,
            )
            full_editor_world = boolean_value(match.group(1)) if match else None

        static_coordinates_baked = boolean_value(
            first_field(
                scene,
                "static_coordinates_baked",
                "static_vertices_baked",
                "coordinates_baked",
                "transforms_baked",
                "static_mesh_vertices_baked",
                "static_mesh_coordinates_baked",
            )
        )
        if static_coordinates_baked is None:
            if re.search(r"(?i)static\s+(?:vertex\s+)?coordinates?\s+(?:are\s+)?not\s+baked", all_text):
                static_coordinates_baked = False
            elif re.search(r"(?i)static\s+(?:vertex\s+)?coordinates?\s+(?:are\s+)?baked", all_text):
                static_coordinates_baked = True

        source_objects = count_from_metadata(
            scene,
            all_text,
            (
                "source_objects",
                "source_object_count",
                "source_mesh_objects",
                "objects_source",
            ),
            "source[ _-]+objects?",
        )
        retained_objects = count_from_metadata(
            scene,
            all_text,
            (
                "retained_objects",
                "retained_object_count",
                "kept_mesh_objects",
                "objects_retained",
            ),
            "retained[ _-]+objects?",
        )
        batch_count = count_from_metadata(
            scene,
            all_text,
            ("batch_count", "batches", "mesh_batch_count", "total_batches"),
            "batches?",
        )
        vertex_count = count_from_metadata(
            scene,
            all_text,
            ("vertex_count", "vertices", "retained_vertices", "total_vertices"),
            "vertices",
        )
        triangle_count = count_from_metadata(
            scene,
            all_text,
            ("triangle_count", "triangles", "retained_triangles", "total_triangles"),
            "triangles",
        )

        candidate, reasons = candidate_assessment(
            width_m=width_m,
            height_m=height_m,
            terrain_count=terrain_count,
            building_count=building_count,
            scene_xml_present=scene_xml is not None,
            blend_present=bool(blend_members),
            transform_explicit=transform_explicit,
        )

        return {
            "town": town,
            "archive_path": str(archive.resolve()),
            "archive_size_bytes": archive.stat().st_size,
            "source_carla_version": source_carla_version,
            "blender_version": blender_version,
            "sionna_versions": sionna_versions,
            "full_editor_world": full_editor_world,
            "retained_bounds_scene_m": bounds,
            "width_m": width_m,
            "height_m": height_m,
            "z_min_m": z_min_m,
            "z_max_m": z_max_m,
            "z_span_m": z_span_m,
            "source_objects": source_objects,
            "retained_objects": retained_objects,
            "batch_count": batch_count,
            "vertex_count": vertex_count,
            "triangle_count": triangle_count,
            "category_counts": counts,
            "building_count": building_count,
            "terrain_count": terrain_count,
            "road_count": road_count,
            "vegetation_count": vegetation_count,
            "ply_file_count": ply_file_count,
            "scene_xml_present": scene_xml is not None,
            "blend_present": bool(blend_members),
            "transforms_xml_present": transforms_info is not None,
            "sha256sums_present": by_suffix["SHA256SUMS"] is not None,
            "coordinate_transform": coordinate_transform,
            "sumo_offset": find_sumo_offset(scene, transform_xml),
            "static_coordinates_baked": static_coordinates_baked,
            "candidate_for_10x10": candidate,
            "candidate_reasons": reasons,
            "inspection_warnings": warnings,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read bounded metadata from a CAVISE Sionna ZIP/ZIP64 bundle."
    )
    parser.add_argument("archive", type=Path, help="CAVISE ... Official ZIP path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.archive.is_file():
        print(f"error: archive does not exist: {args.archive}", file=sys.stderr)
        return 2
    try:
        result = inspect_archive(args.archive)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"error: cannot inspect {args.archive}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
