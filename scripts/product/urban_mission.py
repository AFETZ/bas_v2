"""Small route/frame adapter; all flight control remains in ArduPilot Guided.

The world-to-NED mapping is the declared iris ArduPilotPlugin transform
gazeboXYZToNED=(roll180,yaw90): N=worldY, E=worldX, D=-worldZ.
Ground reference comes from scene/model geometry, never per-run Gazebo poses.
Actual agreement of this transform must be checked by a physical axis pilot.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


def point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("urban coordinate must contain three metres")
    result = tuple(float(x) for x in value)
    if not all(math.isfinite(x) for x in result):
        raise ValueError("urban coordinate must be finite")
    return result


@dataclasses.dataclass(frozen=True)
class NedFrame:
    """A fixed local EKF anchor and a known point in scene geometry."""
    world_anchor: tuple[float, float, float]
    ned_anchor: tuple[float, float, float]

    def to_ned(self, world):
        x, y, z = point(world)
        wx, wy, wz = self.world_anchor
        n, e, d = self.ned_anchor
        return n + y - wy, e + x - wx, d - z + wz

    def to_world(self, ned):
        n, e, d = point(ned)
        an, ae, ad = self.ned_anchor
        x, y, z = self.world_anchor
        return x + e - ae, y + n - an, z - d + ad

    @staticmethod
    def velocity_to_world(ned):
        n, e, d = point(ned)
        return e, n, -d


ROOT = Path(__file__).resolve().parents[2]


def terrain_bundle(raw, root):
    """Verify one prepared bundle; terrain height reuses its exported triangle grid."""
    from scripts.product.prepare_terrain_scene import terrain_height, validate
    import numpy as np
    name = raw.get("terrain_bundle")
    if not isinstance(name, str) or not name:
        raise ValueError("terrain_bundle must be a nonempty workspace-relative path")
    root = root.resolve()
    bundle = (root / name.replace("\\", "/")).resolve()
    bundle.relative_to(root)
    manifest = json.loads((bundle / "scene-manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1 or manifest.get("coordinate_frame") != "ENU"
            or manifest.get("units") != "m" or manifest.get("transforms") != "identity in both consumers"):
        raise ValueError("terrain bundle requires the checked ENU/metre identity geometry contract")
    files = manifest.get("files")
    required = {"source.json", "terrain-grid.npz", "terrain.obj", "buildings.obj", "world.sdf", "scene.xml",
                "licenses/trimesh-MIT.txt"}
    if not isinstance(files, dict) or set(files) != required:
        raise ValueError("terrain bundle is missing its source, shared assets, grid or license")
    for relative, expected in files.items():
        if not isinstance(relative, str):
            raise ValueError("terrain bundle file paths must be strings")
        member = Path(relative)
        if ("\\" in relative or member.is_absolute()
                or ".." in member.parts):
            raise ValueError("terrain bundle file paths must be relative and stay inside the bundle")
        path = (bundle / member).resolve()
        path.relative_to(bundle)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        if (not isinstance(expected, dict) or expected.get("bytes") != path.stat().st_size
                or expected.get("sha256") != digest.hexdigest()):
            raise ValueError(f"terrain bundle file hash/size mismatch: {relative}")
    source = json.loads((bundle / "source.json").read_text(encoding="utf-8"))
    validate(source)
    if manifest.get("scene_id") != source["scene_id"] or manifest.get("origin") != source["origin"]:
        raise ValueError("terrain manifest does not match its source identity/origin")
    frame = raw.get("frame", {})
    for field, key in (("origin_latitude_deg", "latitude_deg"), ("origin_longitude_deg", "longitude_deg"),
                       ("origin_elevation_m", "elevation_m"), ("spherical_heading_deg", "heading_deg")):
        if not math.isclose(float(frame.get(field, math.nan)), source["origin"][key], rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"terrain and mission coordinate origins disagree: {field}")
    if not math.isclose(float(raw["radio"]["frequency_hz"]), source["radio"]["frequency_hz"], rel_tol=0, abs_tol=.5):
        raise ValueError("terrain and mission radio frequencies disagree")
    expected_materials = dict(source["radio"], calibration="uncalibrated ITU reference")
    if manifest.get("materials") != expected_materials:
        raise ValueError("terrain manifest materials disagree with the source")
    # Hashes identify the bundle; also check that both consumers actually refer
    # to those same meshes without a hidden transform or replacement geometry.
    world = ET.parse(bundle / "world.sdf").getroot().find("world")
    rt = ET.parse(bundle / "scene.xml").getroot()
    names = {"terrain", "buildings"}
    if world is None or {m.get("name") for m in world.findall("model")} != names:
        raise ValueError("terrain world must contain exactly the two shared mesh models")
    if {s.get("id") for s in rt.findall("shape")} != {"mesh-" + n for n in names}:
        raise ValueError("terrain RT scene must contain exactly the two shared mesh shapes")
    expected_consumers = {n: dict(gazebo_visual=n + ".obj", gazebo_collision=n + ".obj", sionna=n + ".obj") for n in names}
    if manifest.get("consumer_geometry") != expected_consumers:
        raise ValueError("terrain consumer geometry does not refer to the shared OBJ files")
    for model in world.findall("model"):
        name = model.get("name")
        if model.findtext("static") != "true" or point_pose(model.findtext("pose", "0 0 0 0 0 0")) != (0.,) * 6:
            raise ValueError("terrain mesh models must be static with identity poses")
        links = model.findall("link")
        if len(links) != 1 or point_pose(links[0].findtext("pose", "0 0 0 0 0 0")) != (0.,) * 6:
            raise ValueError("terrain mesh links must have identity poses")
        for kind in ("visual", "collision"):
            shapes = links[0].findall(kind)
            mesh = shapes[0].find("geometry/mesh") if len(shapes) == 1 else None
            if (mesh is None or mesh.findtext("uri") != name + ".obj"
                    or point(mesh.findtext("scale", "1 1 1").split()) != (1., 1., 1.)
                    or point_pose(shapes[0].findtext("pose", "0 0 0 0 0 0")) != (0.,) * 6):
                raise ValueError("terrain collision and visual must use the identity shared mesh")
        shape = rt.find(f"shape[@id='mesh-{name}']")
        material_name = "ground" if name == "terrain" else "building"
        filename = shape.find("string[@name='filename']")
        material_ref = shape.find("ref")
        material = rt.find(f"bsdf[@id='material-{material_name}']")
        material_type = material.find("string[@name='type']") if material is not None else None
        thickness = material.find("float[@name='thickness']") if material is not None else None
        if (shape.get("type") != "obj" or shape.find("transform") is not None
                or filename is None or filename.get("value") != name + ".obj"
                or material_ref is None or material_ref.get("id") != "material-" + material_name
                or material is None or material.get("type") != "itu-radio-material"
                or material_type is None or material_type.get("value") != source["radio"][material_name + "_material"]
                or thickness is None or float(thickness.get("value", "nan")) != source["radio"]["thickness_m"]):
            raise ValueError("terrain RT geometry/material differs from the shared source")
    with np.load(bundle / "terrain-grid.npz", allow_pickle=False) as stored:
        if set(stored.files) != {"x_m", "y_m", "z_m"}:
            raise ValueError("unexpected terrain height-grid fields")
        grid = {key: stored[key] for key in stored.files}
    xs, ys, zs = grid["x_m"], grid["y_m"], grid["z_m"]
    if (xs.ndim != 1 or ys.ndim != 1 or zs.shape != (len(ys), len(xs))
            or len(xs) != source["cells_per_axis"] + 1 or len(ys) != source["cells_per_axis"] + 1
            or not all(np.isfinite(array).all() for array in (xs, ys, zs))
            or not (np.diff(xs) > 0).all() or not (np.diff(ys) > 0).all()):
        raise ValueError("terrain height grid has invalid dimensions, coordinates or values")
    sites = manifest.get("ground_sites", {})
    wanted = {"gcs": raw["gcs"][:2], **{f"uav{i}": point[:2] for i, point in raw["spawns"].items()}}
    for site, xy in wanted.items():
        record = sites.get(site, {})
        if (record.get("xy_m") != xy or not math.isclose(float(record.get("ground_z_m", math.nan)),
                terrain_height(grid, *xy), rel_tol=0, abs_tol=1e-7)):
            raise ValueError(f"terrain sample site does not match the declared endpoint: {site}")
    if point(raw["gcs"])[2] < terrain_height(grid, *raw["gcs"][:2]):
        raise ValueError("GCS antenna position is below the shared terrain")
    if raw.get("buildings"):
        expected_boxes = {name: item["bounds_m"] for name, item in manifest["building_bounds"].items()}
        if {item["id"]: [item["min"], item["max"]] for item in raw["buildings"]} != expected_boxes:
            raise ValueError("flat building overrides disagree with the terrain bundle")
    return bundle, manifest, grid


def point_pose(value):
    values = tuple(float(x) for x in value.split())
    if len(values) != 6 or not all(math.isfinite(x) for x in values):
        raise ValueError("terrain pose must contain six finite values")
    return values


@dataclasses.dataclass(frozen=True)
class UrbanScenario:
    raw: dict
    spawns: dict[int, tuple[float, float, float]]
    routes: dict[int, tuple[tuple[float, float, float], ...]]
    ground_reference_z_m: float
    telemetry_max_age_s: float
    speed_mps: float
    waypoint_dwell_s: float
    barriers: dict[tuple[int, int], dict[int, int]]
    ground_reference_by_uav_m: dict[int, float] | None = None
    terrain_bundle_path: Path | None = None
    terrain_manifest: dict | None = None
    terrain_grid: dict | None = dataclasses.field(default=None, repr=False)
    model_rest_clearance_m: float | None = None
    ground_reference_derivation: str | None = None

    def ground_for(self, uav_id):
        return self.ground_reference_by_uav_m[uav_id] if self.ground_reference_by_uav_m is not None else self.ground_reference_z_m

    @property
    def uav_ids(self):
        return tuple(sorted(self.spawns))

    @classmethod
    def load(cls, path: Path, root: Path = ROOT):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and any(key.startswith("terrain") and key != "terrain_bundle" for key in raw):
            raise ValueError("unknown terrain scene field; use terrain_bundle")
        if not isinstance(raw, dict) or not isinstance(raw.get("spawns"), dict) or not isinstance(raw.get("routes"), dict):
            raise ValueError("urban scenario requires spawns and routes objects")
        count = len(raw["spawns"])
        if not 1 <= count <= 5 or set(raw["spawns"]) != {str(i) for i in range(1, count + 1)}:
            raise ValueError("urban UAV IDs must be the contiguous canonical strings 1..N, N=1..5")
        ids = set(range(1, count + 1))
        if set(raw["routes"]) != set(raw["spawns"]):
            raise ValueError("urban routes must exactly match the declared UAV spawns")
        spawns = {int(i): point(p) for i, p in raw["spawns"].items()}
        routes = {int(i): tuple(point(p) for p in route) for i, route in raw["routes"].items()}
        if set(spawns) != ids or set(routes) != ids or any(not route for route in routes.values()):
            raise ValueError("urban scenario requires spawn and nonempty route for every declared UAV")
        mission = raw["mission"]
        ground, age, speed, dwell = (float(mission[k]) for k in
                ("world_ground_reference_z_m", "telemetry_max_age_s", "speed_mps", "waypoint_dwell_s"))
        if not all(math.isfinite(x) for x in (ground, age, speed, dwell)):
            raise ValueError("urban mission values must be finite")
        if not 0 < age <= .5 or speed <= 0 or dwell <= 0:
            raise ValueError("urban age must be <=0.5 s; speed and dwell must be positive")
        references, bundle, manifest, grid, clearance, derivation = None, None, None, None, None, None
        declared = mission.get("world_ground_reference_z_by_uav_m")
        if declared is not None:
            if not isinstance(declared, dict) or set(declared) != {str(i) for i in ids}:
                raise ValueError("per-UAV ground references must match the exact scene roster")
            references = {int(i): float(value) for i, value in declared.items()}
            if not all(math.isfinite(value) for value in references.values()):
                raise ValueError("per-UAV ground references must be finite")
        if "terrain_bundle" in raw:
            from scripts.product.prepare_terrain_scene import terrain_height
            bundle, manifest, grid = terrain_bundle(raw, root)
            if "model_rest_clearance_m" in mission:
                clearance = mission["model_rest_clearance_m"]
                derivation = "triangle terrain at spawn XY + explicit model_rest_clearance_m"
            else:
                clearance = ground - float(raw["frame"]["ground_surface_z_m"])
                derivation = "triangle terrain at spawn XY + legacy ground reference minus declared flat datum"
            if type(clearance) not in (int, float) or not math.isfinite(clearance) or clearance < 0:
                raise ValueError("terrain model_rest_clearance_m must be finite and nonnegative")
            actual = {i: terrain_height(grid, *spawns[i][:2]) + clearance for i in ids}
            if references is not None and any(not math.isclose(references[i], actual[i], rel_tol=0, abs_tol=1e-7) for i in ids):
                raise ValueError("per-UAV ground references disagree with the shared triangle terrain")
            references = actual
            for i in ids:
                if spawns[i][2] < references[i]:
                    raise ValueError(f"UAV{i} spawn is below its terrain/model ground reference")
                if any(p[2] < terrain_height(grid, *p[:2]) + clearance + 2 for p in routes[i]):
                    raise ValueError(f"UAV{i} waypoint is below terrain/model clearance plus 2 m")
        elif any(p[2] < (references[i] if references is not None else ground) + 2
                 for i, route in routes.items() for p in route):
            raise ValueError("urban flight waypoints must be at least 2 m above declared ground reference")
        # Horizontal frame is explicit and fixed, not inferred from a GPS heading.
        if mission.get("ned_axes", "N=y,E=x,D=-z") != "N=y,E=x,D=-z":
            raise ValueError("unsupported urban NED axes; run a new frame qualification")
        barriers = {}
        for key, value in mission.get("barriers", {}).items():
            try:
                uav, next_index = (int(v) for v in key.split(":"))
                required = {int(i): int(index) for i, index in value["minimum_reached"].items()}
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError("invalid urban barrier; expected uav:next_index and minimum_reached") from exc
            if (uav not in ids or not 0 < next_index < len(routes[uav]) or not required
                    or any(i not in ids or index < 0 or index >= len(routes[i]) for i, index in required.items())
                    or required.get(uav, -1) >= next_index):
                raise ValueError("urban barrier references an invalid or own future waypoint")
            barriers[uav, next_index] = required
        return cls(raw, spawns, routes, ground, age, speed, dwell, barriers,
                   references, bundle, manifest, grid, clearance, derivation)

    def frame(self, uav_id, local_ned_ground):
        spawn = self.spawns[uav_id]
        return NedFrame((spawn[0], spawn[1], self.ground_for(uav_id)), point(local_ned_ground))
