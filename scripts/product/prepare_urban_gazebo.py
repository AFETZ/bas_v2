#!/usr/bin/env python3
"""Generate an idealized Gazebo world and native launch YAML from one geometry.

Only scene/config adaptation: Gazebo and ArduPilot supply flight physics.
The same JSON is read by the native ns-3 building-aware propagation adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[2]


def vector(value):
    if len(value) != 3 or not all(math.isfinite(float(x)) for x in value):
        raise ValueError("expected three finite scene coordinates")
    return [float(x) for x in value]


def intersects(start, end, low, high, margin=0.):
    """Conservative segment/box validation of planned routes, not radio physics."""
    enter, leave = 0., 1.
    for a, b, lo, hi in zip(start, end, low, high):
        lo, hi = lo - margin, hi + margin
        delta = b - a
        if abs(delta) < 1e-12:
            if not lo <= a <= hi:
                return False
            continue
        first, last = sorted(((lo - a) / delta, (hi - a) / delta))
        enter, leave = max(enter, first), min(leave, last)
        if enter > leave:
            return False
    return True


def sub(parent, tag, text=None, **attrs):
    item = ET.SubElement(parent, tag, attrs)
    if text is not None:
        item.text = str(text)
    return item


def box(world, name, low, high, colour):
    size = [b - a for a, b in zip(low, high)]
    centre = [(a + b) / 2 for a, b in zip(low, high)]
    model = sub(world, "model", name=name)
    sub(model, "static", "true")
    sub(model, "pose", " ".join(map(str, centre)) + " 0 0 0")
    link = sub(model, "link", name="body")
    for tag in ("collision", "visual"):
        shape = sub(link, tag, name=tag)
        sub(sub(sub(shape, "geometry"), "box"), "size", " ".join(map(str, size)))
        if tag == "visual":
            material = sub(shape, "material")
            sub(material, "ambient", colour)
            sub(material, "diffuse", colour)


def prepare(config: Path, output: Path):
    data = json.loads(config.read_text())
    ids = {str(i) for i in range(1, 6)}
    if set(data["spawns"]) != ids or set(data["routes"]) != ids:
        raise ValueError("urban scene requires all five spawns/routes")
    buildings = data["buildings"]
    if len({b["id"] for b in buildings}) != len(buildings):
        raise ValueError("duplicate building ID")
    for b in buildings:
        low, high = vector(b["min"]), vector(b["max"])
        if any(a >= c for a, c in zip(low, high)):
            raise ValueError("invalid building volume")
    for uid in sorted(ids):
        points = [vector(data["spawns"][uid])] + [vector(p) for p in data["routes"][uid]]
        for start, end in zip(points, points[1:]):
            for b in buildings:
                if intersects(start, end, b["min"], b["max"], margin=3.):
                    raise ValueError(f"route UAV{uid} intersects building safety envelope {b['id']}")
    output.mkdir(parents=True, exist_ok=True)
    sdf = ET.Element("sdf", version="1.9")
    world = sub(sdf, "world", name="map")
    physics = sub(world, "physics", name="1ms", type="ode")
    for tag, value in (("max_step_size", .001), ("real_time_factor", 1), ("real_time_update_rate", 1000)):
        sub(physics, tag, value)
    systems = {"physics": "Physics", "user-commands": "UserCommands", "scene-broadcaster": "SceneBroadcaster",
               "contact": "Contact", "imu": "Imu", "air-pressure": "AirPressure", "air-speed": "AirSpeed",
               "apply-link-wrench": "ApplyLinkWrench", "magnetometer": "Magnetometer", "navsat": "NavSat"}
    for filename, name in systems.items():
        sub(world, "plugin", filename=f"gz-sim-{filename}-system", name=f"gz::sim::systems::{name}")
    sub(sub(world, "plugin", filename="gz-sim-sensors-system", name="gz::sim::systems::Sensors"), "render_engine", "ogre2")
    sub(sub(world, "scene"), "ambient", ".5 .5 .5 1")
    light = sub(world, "light", name="sun", type="directional")
    sub(light, "direction", ".1 .5 -.8")
    sub(light, "cast_shadows", "false")
    box(world, "urban_ground", [-1000, -1000, -1], [1000, 1000, 0], ".28 .30 .32 1")
    for b in buildings:
        box(world, "urban_building_" + b["id"], b["min"], b["max"], ".55 .57 .62 1")
    frame = data["frame"]
    coords = sub(world, "spherical_coordinates")
    for tag, value in (("surface_model", "EARTH_WGS84"), ("world_frame_orientation", "ENU"),
                       ("latitude_deg", frame["origin_latitude_deg"]), ("longitude_deg", frame["origin_longitude_deg"]),
                       ("elevation", frame["origin_elevation_m"]), ("heading_deg", frame["spherical_heading_deg"])):
        sub(coords, tag, value)
    sub(world, "gravity", "0 0 -9.81")
    ET.indent(sdf)
    ET.ElementTree(sdf).write(output / "world.sdf", encoding="utf-8", xml_declaration=True)
    launch = yaml.safe_load((ROOT / "network/config/scenario_5uav_town01_native_product.yaml").read_text())
    launch["scenario"]["name"] = data["scenario"]
    launch["scenario"]["map"] = dict(id=data["scenario"], kind="idealized_shared_building_boxes", customer_survey_asset=False,
                                      world_file=str((output / "world.sdf").resolve().relative_to(ROOT)), scene_xml="not_used_no_rt.xml",
                                      camera_fragment="network/ns3/runtime_live_cameras.sdf.inc",
                                      gazebo_models=["urban_ground"] + ["urban_building_" + b["id"] for b in buildings])
    launch["command_post"]["position_m"] = vector(data["gcs"])
    for robot in launch["robots"]:
        uid = str(robot["system_id"])
        robot["position"] = vector(data["spawns"][uid]) + [0., 0., 0.]
        robot["nominal_radio_position_m"] = vector(data["spawns"][uid])
    launch["flight"]["missions"] = {f"uav{i}": [dict(name=f"waypoint{k}", position_m=p, hold_s=data["mission"]["waypoint_dwell_s"])
                                                       for k, p in enumerate(data["routes"][i])] for i in sorted(ids)}
    (output / "scenario.yaml").write_text(yaml.safe_dump(launch, sort_keys=False))
    (output / "geometry-source.json").write_text(json.dumps(dict(source=str(config.resolve()), sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                                                                config=data, note="generated scene; no measured flight or radio outcomes"), indent=2) + "\n")
    print(output / "scenario.yaml")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".external/urban_ns3")
    args = parser.parse_args()
    prepare(args.config, args.output)
