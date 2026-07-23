#!/usr/bin/env python3
"""Independently validate the immutable M4 Gazebo/Sionna scene bundle."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = ROOT / "network/config/m4_canonical_scene_bundle.json"
ZERO_SHA256 = "0" * 64
HEX64 = re.compile(r"^[0-9a-f]{64}$")
FRAME_TRANSFORM_VERSION = "ams-m4-coordinate-frames-v1"
GEODETIC_ORIGIN = {
    "datum": "WGS84",
    "elevation_m": 0.0,
    "heading_deg": 0.0,
    "latitude_deg": -35.3632621,
    "longitude_deg": 149.1652374,
}
M4_SITL_SCHEDULER_RATE_HZ = 400.0
M4_SITL_GYRO_RATE_MULTIPLIER = 1.8
M4_SITL_PHYSICS_RATE_HZ = 800.0
M4_SITL_PHYSICS_STEP_S = 1.0 / M4_SITL_PHYSICS_RATE_HZ
IDENTITY_4X4 = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]
NED_DELTA_TO_ENU_DELTA_3X3 = [
    [0.0, 1.0, 0.0],
    [1.0, 0.0, 0.0],
    [0.0, 0.0, -1.0],
]
EXPECTED_ASSETS = {
    "network/config/jammers_m4_canonical.yaml",
    "network/config/radio_m4_canonical.yaml",
    "network/config/scenario_m4_canonical.yaml",
    "src/multiagent_simulation/worlds/m4_canonical/buildings.obj",
    "src/multiagent_simulation/worlds/m4_canonical/landmarks.obj",
    "src/multiagent_simulation/worlds/m4_canonical/low_agl_path.csv",
    "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf",
    "src/multiagent_simulation/worlds/m4_canonical/material_manifest.json",
    "src/multiagent_simulation/worlds/m4_canonical/medium_agl_path.csv",
    "src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml",
    "src/multiagent_simulation/worlds/m4_canonical/terrain.obj",
}


class SceneValidationError(ValueError):
    """Scene evidence is malformed or cannot be independently derived."""


def _unique(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SceneValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def strict_json(path: Path) -> dict[str, Any]:
    try:
        details = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise SceneValidationError(f"cannot read {path}: {exc}") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise SceneValidationError(f"not a single-link regular file: {path}")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SceneValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SceneValidationError(f"JSON root is not an object: {path}")
    return value


def safe_file(root: Path, relative: Any) -> tuple[Path, bytes]:
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise SceneValidationError(f"invalid relative asset path: {relative!r}")
    components = Path(relative).parts
    if any(part in ("", ".", "..") for part in components):
        raise SceneValidationError(f"unsafe asset path: {relative!r}")
    path = root.joinpath(*components)
    current = root
    try:
        for component in components:
            current = current / component
            details = current.lstat()
            if stat.S_ISLNK(details.st_mode):
                raise SceneValidationError(f"symlink in asset path: {relative}")
        details = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise SceneValidationError(f"cannot read asset {relative}: {exc}") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise SceneValidationError(f"asset is not a single-link regular file: {relative}")
    return path, payload


def close(a: float, b: float, tolerance: float = 1e-6) -> bool:
    return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tolerance)


def vector(value: Any, label: str, length: int = 3) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length or not all(finite(item) for item in value):
        raise SceneValidationError(f"{label} must be {length} finite numbers")
    return tuple(float(item) for item in value)


def bounds(value: Any, label: str) -> dict[str, tuple[float, float]]:
    if not isinstance(value, dict) or set(value) != {"x", "y", "z"}:
        raise SceneValidationError(f"{label} must contain exact x/y/z bounds")
    output: dict[str, tuple[float, float]] = {}
    for axis in ("x", "y", "z"):
        pair = vector(value[axis], f"{label}.{axis}", length=2)
        if pair[0] >= pair[1]:
            raise SceneValidationError(f"{label}.{axis} is not increasing")
        output[axis] = (pair[0], pair[1])
    return output


def bounds_equal(actual: Mapping[str, Sequence[float]], expected: Mapping[str, Sequence[float]], tolerance: float = 1e-6) -> bool:
    return all(close(actual[axis][index], expected[axis][index], tolerance) for axis in ("x", "y", "z") for index in (0, 1))


class ObjMesh:
    def __init__(self, payload: bytes, label: str):
        try:
            lines = payload.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise SceneValidationError(f"non-UTF-8 OBJ {label}") from exc
        self.vertices: list[tuple[float, float, float]] = []
        self.normals: list[tuple[float, float, float]] = []
        self.faces: list[tuple[int, int, int]] = []
        self.group_vertices: dict[str, set[int]] = {}
        self.group_faces: dict[str, list[tuple[int, int, int]]] = {}
        current_group = "__ungrouped__"
        for line_number, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("o "):
                continue
            fields = line.split()
            if fields[0] == "g" and len(fields) == 2:
                current_group = fields[1]
                self.group_vertices.setdefault(current_group, set())
                self.group_faces.setdefault(current_group, [])
            elif fields[0] == "v" and len(fields) == 4:
                try:
                    point = tuple(float(item) for item in fields[1:])
                except ValueError as exc:
                    raise SceneValidationError(f"invalid OBJ vertex {label}:{line_number}") from exc
                if not all(math.isfinite(item) for item in point):
                    raise SceneValidationError(f"non-finite OBJ vertex {label}:{line_number}")
                self.vertices.append(point)  # type: ignore[arg-type]
                self.group_vertices.setdefault(current_group, set()).add(len(self.vertices) - 1)
            elif fields[0] == "vn" and len(fields) == 4:
                try:
                    normal = tuple(float(item) for item in fields[1:])
                except ValueError as exc:
                    raise SceneValidationError(
                        f"invalid OBJ normal {label}:{line_number}"
                    ) from exc
                if (
                    not all(math.isfinite(item) for item in normal)
                    or math.sqrt(sum(item * item for item in normal)) < 0.99
                    or math.sqrt(sum(item * item for item in normal)) > 1.01
                ):
                    raise SceneValidationError(
                        f"non-finite/non-unit OBJ normal {label}:{line_number}"
                    )
                self.normals.append(normal)  # type: ignore[arg-type]
            elif fields[0] == "f" and len(fields) == 4:
                try:
                    components = [item.split("/") for item in fields[1:]]
                    if any(
                        len(component) != 3
                        or component[1] != ""
                        or not component[2]
                        for component in components
                    ):
                        raise ValueError("face lacks exact vertex//normal identity")
                    face = tuple(int(component[0]) - 1 for component in components)
                    normal_face = tuple(
                        int(component[2]) - 1 for component in components
                    )
                except ValueError as exc:
                    raise SceneValidationError(f"invalid OBJ face {label}:{line_number}") from exc
                if (
                    min(face) < 0
                    or max(face) >= len(self.vertices)
                    or min(normal_face) < 0
                    or max(normal_face) >= len(self.normals)
                    or len(set(face)) != 3
                    or normal_face != face
                ):
                    raise SceneValidationError(f"unsafe OBJ face {label}:{line_number}")
                self.faces.append(face)  # type: ignore[arg-type]
                self.group_faces.setdefault(current_group, []).append(face)  # type: ignore[arg-type]
            else:
                raise SceneValidationError(f"unsupported OBJ statement {label}:{line_number}: {fields[0]}")
        if not self.vertices or not self.faces:
            raise SceneValidationError(f"empty OBJ geometry: {label}")
        if len(self.normals) != len(self.vertices):
            raise SceneValidationError(
                f"OBJ vertex/normal cardinality differs: {label}"
            )

    def all_bounds(self) -> dict[str, list[float]]:
        return self._indices_bounds(range(len(self.vertices)))

    def group_bounds(self, group: str) -> dict[str, list[float]]:
        indices = self.group_vertices.get(group, set())
        if not indices or not self.group_faces.get(group):
            raise SceneValidationError(f"OBJ group lacks vertices/faces: {group}")
        return self._indices_bounds(indices)

    def _indices_bounds(self, indices: Iterable[int]) -> dict[str, list[float]]:
        points = [self.vertices[index] for index in indices]
        if not points:
            raise SceneValidationError("cannot derive bounds from no vertices")
        return {axis: [min(point[index] for point in points), max(point[index] for point in points)] for index, axis in enumerate(("x", "y", "z"))}

    def terrain_height(self, x_m: float, y_m: float) -> float:
        matches: list[float] = []
        for ia, ib, ic in self.faces:
            a, b, c = self.vertices[ia], self.vertices[ib], self.vertices[ic]
            denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(denominator) < 1e-12:
                continue
            wa = ((b[1] - c[1]) * (x_m - c[0]) + (c[0] - b[0]) * (y_m - c[1])) / denominator
            wb = ((c[1] - a[1]) * (x_m - c[0]) + (a[0] - c[0]) * (y_m - c[1])) / denominator
            wc = 1.0 - wa - wb
            if min(wa, wb, wc) >= -1e-9:
                matches.append(wa * a[2] + wb * b[2] + wc * c[2])
        if not matches:
            raise SceneValidationError(f"terrain mesh does not cover ({x_m},{y_m})")
        return max(matches)


def xml_local_references(payload: bytes, root_tag: str, suffix: str) -> tuple[ET.Element, set[str]]:
    if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
        raise SceneValidationError("XML external declarations are forbidden")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SceneValidationError(f"invalid XML: {exc}") from exc
    if root.tag != root_tag:
        raise SceneValidationError(f"unexpected XML root {root.tag!r}, expected {root_tag!r}")
    references: set[str] = set()
    for element in root.iter():
        candidate = None
        if element.tag == "uri":
            candidate = (element.text or "").strip()
        elif element.tag == "string" and element.attrib.get("name") == "filename":
            candidate = element.attrib.get("value", "").strip()
        if candidate:
            if candidate.startswith("/") or "://" in candidate or Path(candidate).name != candidate:
                raise SceneValidationError(f"nonlocal XML asset reference: {candidate}")
            if candidate.endswith(suffix):
                references.add(candidate)
    return root, references


def segment_intersects_box(start: Sequence[float], end: Sequence[float], box: Mapping[str, Sequence[float]]) -> bool:
    lower = 0.0
    upper = 1.0
    for index, axis in enumerate(("x", "y", "z")):
        delta = end[index] - start[index]
        if abs(delta) < 1e-12:
            if start[index] < box[axis][0] or start[index] > box[axis][1]:
                return False
            continue
        first = (box[axis][0] - start[index]) / delta
        second = (box[axis][1] - start[index]) / delta
        if first > second:
            first, second = second, first
        lower = max(lower, first)
        upper = min(upper, second)
        if lower > upper:
            return False
    return upper >= max(lower, 1e-6) and lower <= 1.0 - 1e-6


def path_obstruction(start: Sequence[float], end: Sequence[float], terrain: ObjMesh, building_boxes: Sequence[Mapping[str, Sequence[float]]]) -> tuple[bool, bool]:
    terrain_blocked = False
    for index in range(401):
        fraction = index / 400.0
        x_m = start[0] + (end[0] - start[0]) * fraction
        y_m = start[1] + (end[1] - start[1]) * fraction
        z_m = start[2] + (end[2] - start[2]) * fraction
        if 0 < index < 400 and terrain.terrain_height(x_m, y_m) >= z_m - 1e-6:
            terrain_blocked = True
            break
    building_blocked = any(segment_intersects_box(start, end, box) for box in building_boxes)
    return terrain_blocked, building_blocked


def make_gate(failures: list[str], details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"passed": not failures, "failures": failures, "details": dict(details or {})}


def expected_coordinate_frame_contract() -> dict[str, Any]:
    """Return the independently frozen four-engine coordinate contract."""

    enu_common = {
        "angle_unit": "rad",
        "axes": ["east", "north", "up"],
        "axis_symbols": ["x", "y", "z"],
        "handedness": "right",
        "position_unit": "m",
        "quaternion_order": "xyzw",
    }
    return {
        "contract": "ams.m4.coordinate-frame-contract/v1",
        "transform_version": FRAME_TRANSFORM_VERSION,
        "origin": {
            "gazebo_world_enu_m": [0.0, 0.0, 0.0],
            "geodetic": GEODETIC_ORIGIN,
            "ardupilot_local_origin_policy": (
                "per_uav_observed_prearm_LOCAL_POSITION_NED_baseline"
            ),
            "ardupilot_relative_alt_origin_policy": (
                "per_uav_observed_prearm_GLOBAL_POSITION_INT_relative_alt_baseline"
            ),
        },
        "frames": {
            "gazebo_world": {"frame_id": "world", **enu_common},
            "ros_odometry": {
                "frame_id": "ros_odometry_world_enu",
                "message_type": "nav_msgs/msg/Odometry",
                "source_topic_pattern": "/uavN/odometry",
                "source_header_frame_id": "odom",
                "source_child_frame_id": "base_link",
                "pose_semantics": "absolute_gazebo_world_pose",
                "gazebo_pose_source": "worldPose(model_entity)",
                "linear_velocity_unit": "m/s",
                "angular_velocity_unit": "rad/s",
                **enu_common,
            },
            "sionna_scene": {"frame_id": "scene", **enu_common},
            "ardupilot_local_position_ned": {
                "frame_id": "ardupilot_local_ned",
                "mavlink_message": "LOCAL_POSITION_NED",
                "mavlink_message_id": 32,
                "axes": ["north", "east", "down"],
                "axis_symbols": ["x", "y", "z"],
                "handedness": "right",
                "position_unit": "m",
                "linear_velocity_unit": "m/s",
                "position_fields": ["x", "y", "z"],
                "linear_velocity_fields": ["vx", "vy", "vz"],
            },
            "ardupilot_global_position_int": {
                "frame_id": "ardupilot_global_wgs84",
                "mavlink_message": "GLOBAL_POSITION_INT",
                "mavlink_message_id": 33,
                "datum": "WGS84",
                "latitude_field": "lat",
                "longitude_field": "lon",
                "latitude_longitude_unit": "1e-7_deg",
                "altitude_msl_field": "alt",
                "altitude_msl_unit": "mm",
                "relative_altitude_field": "relative_alt",
                "relative_altitude_unit": "mm",
                "relative_altitude_reference": "vehicle_home",
                "linear_velocity_fields": ["vx", "vy", "vz"],
                "linear_velocity_unit": "cm/s",
                "heading_field": "hdg",
                "heading_unit": "cdeg",
            },
        },
        "transforms": {
            "gazebo_world_to_ros_odometry": {
                "kind": "homogeneous_position",
                "matrix_4x4": IDENTITY_4X4,
                "version": FRAME_TRANSFORM_VERSION,
            },
            "gazebo_world_to_sionna_scene": {
                "kind": "homogeneous_position",
                "matrix_4x4": IDENTITY_4X4,
                "version": FRAME_TRANSFORM_VERSION,
            },
            "ardupilot_local_ned_delta_to_gazebo_enu_delta": {
                "kind": "linear_delta",
                "matrix_3x3": NED_DELTA_TO_ENU_DELTA_3X3,
                "equation": "enu_delta=[ned_y,ned_x,-ned_z]",
                "version": FRAME_TRANSFORM_VERSION,
            },
            "gazebo_world_enu_to_wgs84": {
                "kind": "gazebo_spherical_coordinates",
                "surface_model": "EARTH_WGS84",
                "origin": GEODETIC_ORIGIN,
                "version": FRAME_TRANSFORM_VERSION,
            },
            "global_relative_altitude_to_gazebo_enu_up_delta": {
                "kind": "scaled_delta",
                "equation": (
                    "enu_up_delta_m=(relative_alt_mm-prearm_relative_alt_mm)/1000"
                ),
                "scale_m_per_input_unit": 0.001,
                "version": FRAME_TRANSFORM_VERSION,
            },
        },
        "runtime_correspondence": {
            "comparison_interval": "[measurement_start,measurement_end)",
            "matching_policy": "nearest_at_or_before",
            "maximum_sample_skew_ns": 1_000_000_000,
            "baseline_policy": "per_uav_observed_prearm_deltas",
            "global_horizontal_max_abs_error_m": 5.0,
            "local_position_max_abs_error_m": 3.0,
            "relative_altitude_max_abs_error_m": 3.0,
        },
        "fixtures": [
            {
                "fixture_id": "local_ned_delta_to_enu_delta",
                "input_local_ned_m": [10.0, 20.0, -30.0],
                "expected_gazebo_enu_m": [20.0, 10.0, 30.0],
            },
            {
                "fixture_id": "relative_altitude_mm_to_enu_up_delta",
                "input_relative_altitude_delta_mm": 30_000,
                "expected_gazebo_enu_up_delta_m": 30.0,
            },
        ],
    }


def validate_scene_bundle(bundle_path: Path = DEFAULT_BUNDLE, root: Path = ROOT) -> dict[str, Any]:
    gates: dict[str, dict[str, Any]] = {}
    try:
        bundle = strict_json(bundle_path)
    except SceneValidationError as exc:
        return {"contract": "ams.m4.scene-bundle-validation/v2", "schema_version": 2, "status": "FAIL", "failures": [str(exc)], "gates": {}}

    identity_failures: list[str] = []
    expected_identity = {
        "contract": "ams.m4.canonical-scene-bundle/v2",
        "schema_version": 2,
        "bundle_id": "ams-m4-canonical-km-v2",
        "bundle_hash_policy": "sha256-canonical-json-with-bundle_sha256-zeroed/v1",
        "transitive_asset_policy": "all-local-regular-files-no-symlinks/v1",
    }
    for field, expected in expected_identity.items():
        if bundle.get(field) != expected:
            identity_failures.append(f"{field} mismatch")
    declared_bundle_hash = bundle.get("bundle_sha256")
    if not isinstance(declared_bundle_hash, str) or not HEX64.fullmatch(declared_bundle_hash):
        identity_failures.append("bundle_sha256 is not lowercase SHA-256")
    else:
        zeroed = copy.deepcopy(bundle)
        zeroed["bundle_sha256"] = ZERO_SHA256
        derived = sha256(canonical_json(zeroed))
        if derived != declared_bundle_hash:
            identity_failures.append("bundle_sha256 does not match canonical zeroed-field derivation")
    gates["identity"] = make_gate(identity_failures, {"bundle_sha256": declared_bundle_hash})

    asset_failures: list[str] = []
    asset_payloads: dict[str, bytes] = {}
    asset_records = bundle.get("assets")
    if not isinstance(asset_records, list):
        asset_failures.append("assets is not a list")
        asset_records = []
    paths: list[str] = []
    for index, record in enumerate(asset_records):
        if not isinstance(record, dict) or set(record) != {"mesh_bounds_m", "path", "role", "sha256", "size_bytes"}:
            asset_failures.append(f"asset[{index}] has wrong fields")
            continue
        path_value = record.get("path")
        if not isinstance(path_value, str):
            asset_failures.append(f"asset[{index}].path invalid")
            continue
        paths.append(path_value)
        try:
            _, payload = safe_file(root, path_value)
            asset_payloads[path_value] = payload
        except SceneValidationError as exc:
            asset_failures.append(str(exc))
            continue
        if record.get("sha256") != sha256(payload):
            asset_failures.append(f"asset hash mismatch: {path_value}")
        if record.get("size_bytes") != len(payload):
            asset_failures.append(f"asset size mismatch: {path_value}")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        asset_failures.append("asset paths are not unique sorted identities")
    if set(paths) != EXPECTED_ASSETS:
        asset_failures.append(f"asset closure differs: missing={sorted(EXPECTED_ASSETS-set(paths))} extra={sorted(set(paths)-EXPECTED_ASSETS)}")
    if bundle.get("asset_manifest_sha256") != sha256(canonical_json(asset_records)):
        asset_failures.append("asset_manifest_sha256 mismatch")
    material_path = "src/multiagent_simulation/worlds/m4_canonical/material_manifest.json"
    if material_path in asset_payloads and bundle.get("scene_material_manifest_sha256") != sha256(asset_payloads[material_path]):
        asset_failures.append("scene_material_manifest_sha256 mismatch")
    gates["asset_closure"] = make_gate(asset_failures, {"asset_count": len(asset_records)})

    mesh_failures: list[str] = []
    meshes: dict[str, ObjMesh] = {}
    for filename in ("terrain.obj", "buildings.obj", "landmarks.obj"):
        relative = f"src/multiagent_simulation/worlds/m4_canonical/{filename}"
        payload = asset_payloads.get(relative)
        if payload is None:
            mesh_failures.append(f"missing mesh bytes: {relative}")
            continue
        try:
            meshes[filename] = ObjMesh(payload, relative)
        except SceneValidationError as exc:
            mesh_failures.append(str(exc))
    for record in asset_records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not record["path"].endswith(".obj"):
            continue
        filename = Path(record["path"]).name
        if filename in meshes:
            try:
                declared = bounds(record.get("mesh_bounds_m"), f"{filename}.mesh_bounds_m")
                if not bounds_equal(meshes[filename].all_bounds(), declared):
                    mesh_failures.append(f"actual mesh bounds mismatch: {filename}")
            except SceneValidationError as exc:
                mesh_failures.append(str(exc))
    gates["mesh_geometry"] = make_gate(mesh_failures, {"mesh_count": len(meshes)})

    scene_ref_failures: list[str] = []
    sdf_path = "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"
    xml_path = "src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml"
    sdf_root = None
    sionna_root = None
    try:
        if bundle.get("gazebo_world") != sdf_path or bundle.get("sionna_scene_xml") != xml_path:
            raise SceneValidationError("canonical Gazebo/Sionna path mismatch")
        sdf_root, sdf_refs = xml_local_references(asset_payloads[sdf_path], "sdf", ".obj")
        sionna_root, sionna_refs = xml_local_references(asset_payloads[xml_path], "scene", ".obj")
        expected_refs = {"terrain.obj", "buildings.obj", "landmarks.obj"}
        if sdf_refs != expected_refs or sionna_refs != expected_refs:
            raise SceneValidationError(f"shared mesh references differ: gazebo={sorted(sdf_refs)} sionna={sorted(sionna_refs)}")
        material = json.loads(asset_payloads[material_path].decode("utf-8"), object_pairs_hook=_unique)
        material_ids = {item["id"] for item in material.get("materials", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        sionna_bsdfs = {item.attrib.get("id") for item in sionna_root.findall("bsdf")}
        sionna_material_refs = {item.attrib.get("id") for item in sionna_root.iter("ref")}
        if material_ids != sionna_bsdfs or material_ids != sionna_material_refs:
            raise SceneValidationError("material manifest and Sionna BSDF/reference sets differ")
    except (KeyError, json.JSONDecodeError, SceneValidationError) as exc:
        scene_ref_failures.append(str(exc))
    gates["shared_scene_references"] = make_gate(scene_ref_failures)

    physics_failures: list[str] = []
    try:
        if sdf_root is None:
            raise SceneValidationError("Gazebo SDF was not parsed for physics validation")
        world = sdf_root.find("world")
        physics = world.find("physics") if world is not None else None
        if physics is None or physics.attrib != {
            "name": "m4_capacity_physics",
            "type": "ode",
        }:
            raise SceneValidationError("M4 Gazebo physics identity differs")
        step_s = float(physics.findtext("max_step_size") or "nan")
        update_rate_hz = float(physics.findtext("real_time_update_rate") or "nan")
        if not close(step_s, M4_SITL_PHYSICS_STEP_S) or not close(
            update_rate_hz, M4_SITL_PHYSICS_RATE_HZ
        ):
            raise SceneValidationError("M4 Gazebo physics rate differs")
        if (1.0 / step_s) < (
            M4_SITL_SCHEDULER_RATE_HZ * M4_SITL_GYRO_RATE_MULTIPLIER
        ):
            raise SceneValidationError("M4 Gazebo gyro source is below ArduPilot pre-arm minimum")
    except (TypeError, ValueError, SceneValidationError) as exc:
        physics_failures.append(str(exc))
    gates["gazebo_physics"] = make_gate(
        physics_failures,
        {
            "max_step_size_s": M4_SITL_PHYSICS_STEP_S,
            "real_time_update_rate_hz": M4_SITL_PHYSICS_RATE_HZ,
        },
    )

    entity_failures: list[str] = []
    try:
        if sdf_root is None:
            raise SceneValidationError("Gazebo SDF was not parsed")
        world = sdf_root.find("world")
        if world is None or world.attrib.get("name") != "map":
            raise SceneValidationError("Gazebo world identity differs")
        model_by_name = {
            model.attrib.get("name"): model for model in world.findall("model")
        }
        expected_entities = {
            "cp": (-8000.0, -2500.0, 300.0, 0.0, 0.0, 0.0),
            "jammer_m4": (2000.0, -3000.0, 100.0, 0.0, 0.0, 0.0),
        }
        for name, expected_pose in expected_entities.items():
            model = model_by_name.get(name)
            if model is None:
                raise SceneValidationError(f"Gazebo entity is absent: {name}")
            if (model.findtext("static") or "").strip() != "true":
                raise SceneValidationError(f"Gazebo entity is not initially static: {name}")
            pose_text = (model.findtext("pose") or "").split()
            if len(pose_text) != 6:
                raise SceneValidationError(f"Gazebo entity pose is incomplete: {name}")
            pose = tuple(float(value) for value in pose_text)
            if any(not close(pose[index], expected_pose[index]) for index in range(6)):
                raise SceneValidationError(f"Gazebo entity initial pose differs: {name}")
            if model.findall(".//collision"):
                raise SceneValidationError(
                    f"radio marker must not alter collision geometry: {name}"
                )
    except (TypeError, ValueError, SceneValidationError) as exc:
        entity_failures.append(str(exc))
    gates["gazebo_radio_entities"] = make_gate(entity_failures)

    frame_failures: list[str] = []
    try:
        frame = bundle.get("coordinate_frame")
        expected_legacy_frame = {
            "axes": "ENU",
            "gazebo_frame": "world",
            "origin": {
                "elevation_m": GEODETIC_ORIGIN["elevation_m"],
                "latitude_deg": GEODETIC_ORIGIN["latitude_deg"],
                "longitude_deg": GEODETIC_ORIGIN["longitude_deg"],
            },
            "sionna_frame": "scene",
            "units": "m",
        }
        if frame != expected_legacy_frame:
            raise SceneValidationError("coordinate_frame is not exact shared ENU metre frame")
        frame_contract = bundle.get("frame_contract")
        expected_frame_contract = expected_coordinate_frame_contract()
        if frame_contract != expected_frame_contract:
            raise SceneValidationError(
                "Gazebo/ROS/ArduPilot/Sionna coordinate-frame contract differs"
            )
        matrix = bundle.get("gazebo_to_sionna_transform_matrix")
        if bundle.get("gazebo_to_sionna_transform_version") != "enu-identity-v1" or matrix != IDENTITY_4X4:
            raise SceneValidationError("Gazebo-to-Sionna transform is not the frozen identity")

        if sdf_root is None:
            raise SceneValidationError("Gazebo SDF was not parsed for frame validation")
        frame_world = sdf_root.find("world")
        spherical = (
            frame_world.find("spherical_coordinates")
            if frame_world is not None
            else None
        )
        if spherical is None:
            raise SceneValidationError("Gazebo WGS84 spherical origin is absent")
        spherical_values = {
            "datum": (spherical.findtext("surface_model") or "").strip(),
            "elevation_m": float(spherical.findtext("elevation") or "nan"),
            "heading_deg": float(spherical.findtext("heading_deg") or "nan"),
            "latitude_deg": float(spherical.findtext("latitude_deg") or "nan"),
            "longitude_deg": float(spherical.findtext("longitude_deg") or "nan"),
        }
        if (
            spherical_values["datum"]
            != f"EARTH_{GEODETIC_ORIGIN['datum']}"
            or any(
                not close(
                    spherical_values[field],
                    float(GEODETIC_ORIGIN[field]),
                    1e-9,
                )
                for field in (
                    "elevation_m",
                    "heading_deg",
                    "latitude_deg",
                    "longitude_deg",
                )
            )
        ):
            raise SceneValidationError(
                "Gazebo spherical origin differs from the WGS84 frame contract"
            )

        # Independently execute the two conversion fixtures.  Exact dictionary
        # equality above freezes labels/units/tolerances; these calculations
        # additionally reject a self-consistent but mathematically false claim.
        fixtures = {
            item["fixture_id"]: item
            for item in frame_contract["fixtures"]
            if isinstance(item, dict) and isinstance(item.get("fixture_id"), str)
        }
        if set(fixtures) != {
            "local_ned_delta_to_enu_delta",
            "relative_altitude_mm_to_enu_up_delta",
        }:
            raise SceneValidationError("coordinate conversion fixture set differs")
        ned_fixture = fixtures["local_ned_delta_to_enu_delta"]
        ned_input = vector(
            ned_fixture["input_local_ned_m"], "frame_fixture.local_ned"
        )
        derived_enu = tuple(
            sum(row[column] * ned_input[column] for column in range(3))
            for row in NED_DELTA_TO_ENU_DELTA_3X3
        )
        expected_enu = vector(
            ned_fixture["expected_gazebo_enu_m"], "frame_fixture.enu"
        )
        if any(not close(actual, expected) for actual, expected in zip(derived_enu, expected_enu)):
            raise SceneValidationError("NED-to-ENU conversion fixture is false")
        # A proper axis transformation preserves handedness (determinant +1).
        ned_to_enu = NED_DELTA_TO_ENU_DELTA_3X3
        determinant = (
            ned_to_enu[0][0]
            * (ned_to_enu[1][1] * ned_to_enu[2][2] - ned_to_enu[1][2] * ned_to_enu[2][1])
            - ned_to_enu[0][1]
            * (ned_to_enu[1][0] * ned_to_enu[2][2] - ned_to_enu[1][2] * ned_to_enu[2][0])
            + ned_to_enu[0][2]
            * (ned_to_enu[1][0] * ned_to_enu[2][1] - ned_to_enu[1][1] * ned_to_enu[2][0])
        )
        if not close(determinant, 1.0):
            raise SceneValidationError("NED-to-ENU transform is not right-handed")
        altitude_fixture = fixtures["relative_altitude_mm_to_enu_up_delta"]
        altitude_scale = frame_contract["transforms"][
            "global_relative_altitude_to_gazebo_enu_up_delta"
        ]["scale_m_per_input_unit"]
        derived_altitude_m = (
            altitude_fixture["input_relative_altitude_delta_mm"] * altitude_scale
        )
        if not close(
            derived_altitude_m,
            altitude_fixture["expected_gazebo_enu_up_delta_m"],
        ):
            raise SceneValidationError("relative-altitude conversion fixture is false")
        operating = bundle.get("operating_bounds_m")
        if not isinstance(operating, dict) or set(operating) != {"collision_usable", "rf_usable"}:
            raise SceneValidationError("operating_bounds_m fields differ")
        collision = bounds(operating["collision_usable"], "collision_usable")
        radio = bounds(operating["rf_usable"], "rf_usable")
        for label, envelope in (("collision", collision), ("rf", radio)):
            if envelope["x"][1] - envelope["x"][0] < 20000.0 or envelope["y"][1] - envelope["y"][0] < 10000.0:
                raise SceneValidationError(f"{label} operating region is smaller than 20km x 10km")
        if collision != radio:
            raise SceneValidationError("collision and RF usable bounds differ")
        if "terrain.obj" in meshes:
            terrain_bounds = meshes["terrain.obj"].all_bounds()
            if not close(terrain_bounds["x"][0], collision["x"][0]) or not close(terrain_bounds["x"][1], collision["x"][1]) or not close(terrain_bounds["y"][0], collision["y"][0]) or not close(terrain_bounds["y"][1], collision["y"][1]):
                raise SceneValidationError("terrain mesh does not span operating x/y bounds")
    except (KeyError, TypeError, ValueError, SceneValidationError) as exc:
        frame_failures.append(str(exc))
    gates["frames_and_bounds"] = make_gate(
        frame_failures,
        {
            "frame_contract": "ams.m4.coordinate-frame-contract/v1",
            "transform_version": FRAME_TRANSFORM_VERSION,
        },
    )

    relief_failures: list[str] = []
    try:
        terrain = meshes["terrain.obj"]
        relief = bundle.get("relief")
        if not isinstance(relief, dict) or set(relief) != {"delta_m", "high_fixture", "low_fixture"}:
            raise SceneValidationError("relief contract fields differ")
        low = vector(relief["low_fixture"].get("position_m"), "relief.low")
        high = vector(relief["high_fixture"].get("position_m"), "relief.high")
        if not close(terrain.terrain_height(low[0], low[1]), low[2]) or not close(terrain.terrain_height(high[0], high[1]), high[2]):
            raise SceneValidationError("relief fixture is not on the actual terrain mesh")
        derived = high[2] - low[2]
        if not close(derived, relief.get("delta_m")) or not (150.0 <= derived <= 200.0):
            raise SceneValidationError("terrain relief is outside 150..200 m or not derived")
    except (KeyError, AttributeError, SceneValidationError, TypeError) as exc:
        relief_failures.append(str(exc))
    gates["terrain_relief"] = make_gate(relief_failures)

    settlement_failures: list[str] = []
    building_boxes: list[dict[str, list[float]]] = []
    try:
        clusters = bundle.get("building_clusters")
        if not isinstance(clusters, list) or len(clusters) < 2:
            raise SceneValidationError("fewer than two building clusters")
        cluster_ids: set[str] = set()
        building_ids: set[str] = set()
        has_required_highrise = False
        for cluster in clusters:
            if not isinstance(cluster, dict) or set(cluster) != {"buildings", "id", "required_classes"}:
                raise SceneValidationError("building cluster fields differ")
            cluster_id = cluster["id"]
            if not isinstance(cluster_id, str) or cluster_id in cluster_ids:
                raise SceneValidationError("duplicate/invalid building cluster ID")
            cluster_ids.add(cluster_id)
            records = cluster["buildings"]
            classes = {record.get("class") for record in records if isinstance(record, dict)}
            if cluster.get("required_classes") != ["low", "medium", "high"] or classes != {"low", "medium", "high"}:
                raise SceneValidationError(f"cluster {cluster_id} lacks low/medium/high geometry")
            for record in records:
                if not isinstance(record, dict) or set(record) != {"bounds_m", "class", "floor_height_m", "floors", "height_m", "id"}:
                    raise SceneValidationError("building record fields differ")
                building_id = record["id"]
                if not isinstance(building_id, str) or building_id in building_ids:
                    raise SceneValidationError("duplicate/invalid building ID")
                building_ids.add(building_id)
                box = bounds(record["bounds_m"], f"building.{building_id}")
                if not isinstance(record["floors"], int) or isinstance(record["floors"], bool) or not finite(record["floor_height_m"]):
                    raise SceneValidationError(f"invalid floor convention for {building_id}")
                derived_height = record["floors"] * float(record["floor_height_m"])
                if not close(derived_height, record["height_m"]) or not close(box["z"][1] - box["z"][0], derived_height):
                    raise SceneValidationError(f"height/floor mismatch for {building_id}")
                actual = meshes["buildings.obj"].group_bounds(building_id)
                if not bounds_equal(actual, box):
                    raise SceneValidationError(f"OBJ group bounds mismatch for {building_id}")
                building_boxes.append({axis: list(pair) for axis, pair in box.items()})
                if record["class"] == "high" and 12 <= record["floors"] <= 15:
                    has_required_highrise = True
        if not has_required_highrise:
            raise SceneValidationError("no explicit 12..15-storey high-rise fixture")
    except (KeyError, TypeError, SceneValidationError) as exc:
        settlement_failures.append(str(exc))
    gates["building_clusters"] = make_gate(settlement_failures, {"building_count": len(building_boxes)})

    landmark_failures: list[str] = []
    try:
        landmarks = bundle.get("landmarks")
        if not isinstance(landmarks, list) or len(landmarks) < 6:
            raise SceneValidationError("fewer than six alignment landmarks")
        ids: set[str] = set()
        x_values: list[float] = []
        z_values: set[float] = set()
        for item in landmarks:
            if not isinstance(item, dict) or set(item) != {"expected_enu_m", "gazebo_sample_m", "id", "max_error_m", "sionna_sample_m"}:
                raise SceneValidationError("landmark fields differ")
            landmark_id = item["id"]
            if not isinstance(landmark_id, str) or landmark_id in ids:
                raise SceneValidationError("duplicate/invalid landmark ID")
            ids.add(landmark_id)
            expected = vector(item["expected_enu_m"], f"landmark.{landmark_id}.expected")
            gazebo = vector(item["gazebo_sample_m"], f"landmark.{landmark_id}.gazebo")
            sionna = vector(item["sionna_sample_m"], f"landmark.{landmark_id}.sionna")
            maximum = float(item["max_error_m"])
            if maximum > 1.0 or maximum < 0.0:
                raise SceneValidationError(f"landmark tolerance invalid for {landmark_id}")
            for label, sample in (("Gazebo", gazebo), ("Sionna", sionna)):
                error = math.dist(expected, sample)
                if error > maximum:
                    raise SceneValidationError(f"{label} landmark error exceeds 1m for {landmark_id}")
            group_bounds = meshes["landmarks.obj"].group_bounds(f"landmark_{landmark_id}")
            center = tuple((group_bounds[axis][0] + group_bounds[axis][1]) / 2.0 for axis in ("x", "y", "z"))
            if math.dist(center, expected) > maximum:
                raise SceneValidationError(f"actual landmark mesh center differs for {landmark_id}")
            x_values.append(expected[0])
            z_values.add(expected[2])
        if min(x_values) > -9000.0 or max(x_values) < 9000.0 or len(z_values) < 3:
            raise SceneValidationError("landmarks do not span both horizontal extremes and three elevations")
    except (KeyError, TypeError, SceneValidationError) as exc:
        landmark_failures.append(str(exc))
    gates["landmark_alignment"] = make_gate(landmark_failures, {"landmark_count": len(bundle.get("landmarks", [])) if isinstance(bundle.get("landmarks"), list) else 0})

    agl_failures: list[str] = []
    agl_details: dict[str, Any] = {}
    try:
        terrain = meshes["terrain.obj"]
        agl_paths = bundle.get("agl_paths")
        if not isinstance(agl_paths, dict) or set(agl_paths) != {"low", "medium"}:
            raise SceneValidationError("AGL path set differs")
        for profile in ("low", "medium"):
            contract = agl_paths[profile]
            if not isinstance(contract, dict) or set(contract) != {"agl_bounds_m", "expected_agl_m", "length_m", "path", "sample_spacing_m", "sha256"}:
                raise SceneValidationError(f"{profile} AGL contract fields differ")
            path_value = contract["path"]
            payload = asset_payloads.get(path_value)
            if payload is None or contract["sha256"] != sha256(payload):
                raise SceneValidationError(f"{profile} AGL artifact hash mismatch")
            rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))
            expected_fields = ["distance_m", "x_m", "y_m", "terrain_z_m", "altitude_z_m", "agl_m"]
            if not rows or list(rows[0]) != expected_fields:
                raise SceneValidationError(f"{profile} AGL CSV header differs")
            spacings: list[float] = []
            valid_count = 0
            previous_distance: float | None = None
            lower, upper = vector(contract["agl_bounds_m"], f"{profile}.agl_bounds", length=2)
            for row in rows:
                values = {key: float(row[key]) for key in expected_fields}
                if not all(math.isfinite(value) for value in values.values()):
                    raise SceneValidationError(f"non-finite {profile} AGL sample")
                derived_ground = terrain.terrain_height(values["x_m"], values["y_m"])
                if not close(derived_ground, values["terrain_z_m"], 1e-5):
                    raise SceneValidationError(f"{profile} terrain sample not derived from mesh")
                derived_agl = values["altitude_z_m"] - derived_ground
                if not close(derived_agl, values["agl_m"], 1e-5):
                    raise SceneValidationError(f"{profile} AGL arithmetic mismatch")
                if derived_agl < 20.0 - 1e-6:
                    raise SceneValidationError(f"{profile} sample below 20m AGL")
                if lower - 1e-6 <= derived_agl <= upper + 1e-6:
                    valid_count += 1
                if previous_distance is not None:
                    spacings.append(values["distance_m"] - previous_distance)
                previous_distance = values["distance_m"]
            if float(rows[-1]["distance_m"]) - float(rows[0]["distance_m"]) < 1000.0 or not close(contract["length_m"], 1000.0):
                raise SceneValidationError(f"{profile} path is shorter than 1km")
            if any(spacing <= 0.0 or spacing > 25.0 + 1e-6 for spacing in spacings) or not close(contract["sample_spacing_m"], 25.0):
                raise SceneValidationError(f"{profile} sample spacing exceeds 25m")
            ratio = valid_count / len(rows)
            if ratio < 0.95:
                raise SceneValidationError(f"{profile} AGL band coverage below 95%")
            agl_details[profile] = {"samples": len(rows), "band_ratio": ratio}
    except (KeyError, TypeError, ValueError, UnicodeError, SceneValidationError) as exc:
        agl_failures.append(str(exc))
    gates["agl_corridors"] = make_gate(agl_failures, agl_details)

    range_failures: list[str] = []
    range_details: dict[str, Any] = {}
    try:
        terrain = meshes["terrain.obj"]
        fixtures = bundle.get("range_fixtures")
        if not isinstance(fixtures, list):
            raise SceneValidationError("range_fixtures is not a list")
        expected_keys = {(geometry, distance) for geometry in ("los", "obstructed") for distance in (1000.0, 5000.0, 10000.0, 20000.0)}
        actual_keys: set[tuple[str, float]] = set()
        ids: set[str] = set()
        for fixture in fixtures:
            if not isinstance(fixture, dict) or set(fixture) != {"distance_m", "geometry", "id", "rx_position_m", "tx_position_m"}:
                raise SceneValidationError("range fixture fields differ")
            fixture_id = fixture["id"]
            if not isinstance(fixture_id, str) or fixture_id in ids:
                raise SceneValidationError("duplicate/invalid range fixture ID")
            ids.add(fixture_id)
            start = vector(fixture["tx_position_m"], f"{fixture_id}.tx")
            end = vector(fixture["rx_position_m"], f"{fixture_id}.rx")
            distance = float(fixture["distance_m"])
            if not close(math.dist(start, end), distance, 1e-3):
                raise SceneValidationError(f"range distance not derived for {fixture_id}")
            geometry = fixture["geometry"]
            normalized = "los" if geometry == "los" else "obstructed"
            if geometry not in {"los", "terrain_shadow", "building_blocked"}:
                raise SceneValidationError(f"unknown geometry for {fixture_id}")
            key = (normalized, distance)
            if key in actual_keys:
                raise SceneValidationError(f"duplicate range geometry/distance key {key}")
            actual_keys.add(key)
            terrain_blocked, building_blocked = path_obstruction(start, end, terrain, building_boxes)
            if geometry == "los" and (terrain_blocked or building_blocked):
                raise SceneValidationError(f"declared LoS fixture is obstructed: {fixture_id}")
            if geometry == "terrain_shadow" and not terrain_blocked:
                raise SceneValidationError(f"terrain-shadow fixture lacks actual terrain obstruction: {fixture_id}")
            if geometry == "building_blocked" and not building_blocked:
                raise SceneValidationError(f"building-blocked fixture lacks actual building obstruction: {fixture_id}")
            range_details[fixture_id] = {"terrain_blocked": terrain_blocked, "building_blocked": building_blocked}
        if actual_keys != expected_keys:
            raise SceneValidationError(f"range geometry matrix differs: missing={sorted(expected_keys-actual_keys)} extra={sorted(actual_keys-expected_keys)}")
    except (KeyError, TypeError, SceneValidationError) as exc:
        range_failures.append(str(exc))
    gates["range_geometry"] = make_gate(range_failures, range_details)

    causal_failures: list[str] = []
    try:
        terrain = meshes["terrain.obj"]
        scenarios = bundle.get("causal_scenarios")
        if not isinstance(scenarios, dict) or set(scenarios) != {"building_blocked", "jammer_off_on_off", "terrain_shadow"}:
            raise SceneValidationError("causal scenario set differs")
        for name, expected_sequence in (("terrain_shadow", ["terrain_good", "terrain_down", "terrain_recovery"]), ("building_blocked", ["building_good", "building_down", "building_recovery"])):
            scenario = scenarios[name]
            if scenario.get("sequence") != expected_sequence or not isinstance(scenario.get("pose_sets"), dict):
                raise SceneValidationError(f"{name} sequence differs")
            target = scenario.get("target_link")
            control = scenario.get("control_link")
            if target not in {"cp>uav1", "cp>uav2"} or control != "cp>uav5":
                raise SceneValidationError(f"{name} link assignment differs")
            target_node = target.split(">", 1)[1]
            observations: list[tuple[bool, bool]] = []
            for pose_id in expected_sequence:
                pose_set_value = scenario["pose_sets"].get(pose_id)
                if not isinstance(pose_set_value, dict) or set(pose_set_value) != {"cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4"}:
                    raise SceneValidationError(f"{name}/{pose_id} pose set differs")
                start = vector(pose_set_value["cp"], f"{pose_id}.cp")
                end = vector(pose_set_value[target_node], f"{pose_id}.{target_node}")
                observations.append(path_obstruction(start, end, terrain, building_boxes))
            if observations[0] != (False, False) or observations[2] != (False, False):
                raise SceneValidationError(f"{name} good/recovery geometry is not clear")
            if name == "terrain_shadow" and not observations[1][0]:
                raise SceneValidationError("terrain down pose lacks terrain shadow")
            if name == "building_blocked" and not observations[1][1]:
                raise SceneValidationError("building down pose lacks building blockage")
        jammer = scenarios["jammer_off_on_off"]
        fixture = bundle.get("jammer_fixture")
        if jammer.get("sequence") != ["off-1", "on", "off-2"] or jammer.get("target_link") != "cp>uav3" or jammer.get("control_link") != "cp>uav5":
            raise SceneValidationError("jammer causal phase contract differs")
        if not isinstance(fixture, dict) or fixture.get("id") != "jammer_m4" or fixture.get("center_hz") != 2400000000 or fixture.get("bandwidth_hz") != 20000000 or fixture.get("duty_cycle") != 1.0 or not finite(fixture.get("power_dbm")):
            raise SceneValidationError("jammer fixture differs or is not continuous co-channel")
        jammer_pose = vector(fixture.get("position_m"), "jammer.position")
        if vector(jammer["pose_set"].get("jammer_m4"), "jammer.pose_set") != jammer_pose:
            raise SceneValidationError("jammer phase pose and fixture differ")
    except (KeyError, AttributeError, TypeError, SceneValidationError) as exc:
        causal_failures.append(str(exc))
    gates["causal_geometry"] = make_gate(causal_failures)

    config_failures: list[str] = []
    try:
        scenario = yaml.safe_load(asset_payloads["network/config/scenario_m4_canonical.yaml"])
        radio = yaml.safe_load(asset_payloads["network/config/radio_m4_canonical.yaml"])
        jammers = yaml.safe_load(asset_payloads["network/config/jammers_m4_canonical.yaml"])
        if (
            scenario["scenario"]["name"] != "scenario_m4_canonical"
            or scenario["scenario"]["map"]["world_file"]
            != "m4_canonical/m4_canonical.sdf"
            or scenario["scenario"]["map"]["size_m"] != [20000, 10000]
            or scenario["scenario"]["map"]["terrain_height_variation_m"] != 180
            or scenario["scenario"]["map"]["building_height_limit_floors"] != 15
            or scenario["base_simulation"].get("sitl_home")
            != "-35.3632621,149.1652374,0,0"
        ):
            raise SceneValidationError(
                "flight scenario does not bind final scene envelope/WGS84 SITL home"
            )
        robots = scenario.get("robots", [])
        if len(robots) != 5 or {item.get("name") for item in robots} != {f"uav{index}" for index in range(1, 6)} or {item.get("system_id") for item in robots} != set(range(1, 6)):
            raise SceneValidationError("flight scenario does not contain exact five UAV identities")
        terrain = meshes["terrain.obj"]
        for robot in robots:
            spawn = vector(robot.get("position", [])[:3], f"{robot.get('name')}.spawn")
            ground = terrain.terrain_height(spawn[0], spawn[1])
            if not close(spawn[2], ground, 1e-5):
                raise SceneValidationError(
                    f"{robot.get('name')} spawn is not on the actual collision terrain"
                )
            radio_position = vector(
                robot.get("nominal_radio_position_m"),
                f"{robot.get('name')}.nominal_radio_position",
            )
            if radio_position[2] <= ground + 20.0:
                raise SceneValidationError(
                    f"{robot.get('name')} nominal radio pose is not above terrain"
                )
        scene = radio["sionna"]["scene"]
        if scene != {"id": bundle.get("bundle_id"), "source": "mitsuba_xml", "path": bundle.get("sionna_scene_xml")} or radio["sionna"].get("required_for_acceptance") is not True or radio["ns3"].get("require_sionna") is not True:
            raise SceneValidationError("radio config does not require exact real canonical Sionna scene")
        if (
            radio["ns3"].get("sionna_query_period_s") != 1.0
            or radio["ns3"].get("sionna_deadline_ms") != 100
            or radio["radio"].get("carrier_hz") != 2400000000
            or radio["radio"].get("bandwidth_hz") != 20000000
        ):
            raise SceneValidationError("radio timing/frequency policy differs")
        if radio["sionna"].get("solver", {}).get("surface_epsilon_m") != 0.001:
            raise SceneValidationError(
                "radio solver does not bind the terrain-boundary ray-origin epsilon"
            )
        expected_mapping = [
            {"min_sinr_db": 20.0, "service_tier_bps": 20000000, "link_state": "excellent", "per_input": 0.001},
            {"min_sinr_db": 11.0, "service_tier_bps": 2000000, "link_state": "good", "per_input": 0.005},
            {"min_sinr_db": 6.0, "service_tier_bps": 500000, "link_state": "usable", "per_input": 0.02},
            {"min_sinr_db": 0.0, "service_tier_bps": 100000, "link_state": "marginal", "per_input": 0.08},
            {"min_sinr_db": -4.0, "service_tier_bps": 10000, "link_state": "degraded", "per_input": 0.25},
            {"min_sinr_db": -8.0, "service_tier_bps": 1000, "link_state": "critical_only", "per_input": 0.5},
            {"min_sinr_db": -999.0, "service_tier_bps": 0, "link_state": "down", "per_input": 1.0},
        ]
        if radio.get("service_tier_selection") != expected_mapping:
            raise SceneValidationError(
                "radio config does not freeze the complete six-tier boundary mapping"
            )
        heatmaps = radio.get("heatmaps")
        if (
            not isinstance(heatmaps, dict)
            or heatmaps.get("default_grid_points") != 51
            or heatmaps.get("extent_m")
            != [-10000.0, 10000.0, -5000.0, 5000.0]
        ):
            raise SceneValidationError("heatmap grid does not bind the canonical RF bounds")
        jammer_records = jammers.get("jammers", [])
        if len(jammer_records) != 1 or jammer_records[0].get("id") != bundle.get("jammer_fixture", {}).get("id") or jammer_records[0].get("position_m") != bundle.get("jammer_fixture", {}).get("position_m"):
            raise SceneValidationError("jammer runtime config and scene fixture differ")
        if (
            jammer_records[0].get("enabled") is not False
            or jammer_records[0].get("time_behavior") != "runtime_off_on_off"
        ):
            raise SceneValidationError(
                "jammer baseline must start disabled for the off/on/off causal contract"
            )
    except (KeyError, TypeError, yaml.YAMLError, SceneValidationError) as exc:
        config_failures.append(str(exc))
    gates["runtime_configs"] = make_gate(config_failures)

    failures = [f"{name}: {failure}" for name, result in gates.items() for failure in result["failures"]]
    return {
        "bundle_id": bundle.get("bundle_id"),
        "bundle_sha256": bundle.get("bundle_sha256"),
        "contract": "ams.m4.scene-bundle-validation/v2",
        "failures": failures,
        "gates": gates,
        "schema_version": 2,
        "status": "PASS" if not failures else "FAIL",
    }


def write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json-output", type=Path)
    output.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    result = validate_scene_bundle(args.bundle.resolve(), ROOT)
    payload = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode()
    if args.json_output:
        write_new(args.json_output, payload)
    else:
        print(payload.decode(), end="")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
