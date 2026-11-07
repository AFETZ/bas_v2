#!/usr/bin/env python3
"""Generate a Gazebo world and native launch YAML from one shared geometry.

Only scene/config adaptation: Gazebo and ArduPilot supply flight physics.
Uses idealized boxes or a verified shared Gazebo/Sionna terrain mesh bundle.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.product.urban_mission import UrbanScenario


def read_rt_radio_profile(path: Path, scene: dict):
    """Check the explicit native Wi-Fi/Sionna profile before launching any simulator."""
    class UniqueSafeLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            values = {}
            for key, item in self.construct_pairs(node, deep=deep):
                if not isinstance(key, str):
                    raise ValueError("radio profile field names must be strings")
                if key in values:
                    raise ValueError(f"duplicate radio profile field: {key}")
                values[key] = item
            return values
    try:
        value = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=UniqueSafeLoader)
    except yaml.YAMLError as error:
        raise ValueError("invalid radio profile YAML") from error
    keys = {
        "radio": {"backend", "profile", "technology_specific_modem", "standard", "calibration",
                  "non_unicast_mode", "antenna", "control_basic_rates", "ssid", "channel_number",
                  "carrier_hz", "channel_width_mhz", "data_mode", "control_mode", "tx_power_w",
                  "legacy_aloha_phy_rate_bps", "shared_spectrum_channels", "neighbor_discovery_mode", "reason"},
        "sionna": {"solver_profile", "receiver_batch_mode", "execution_mode", "worker_count",
                   "max_depth", "los", "specular_reflection", "diffuse_reflection",
                   "diffraction", "edge_diffraction", "refraction", "synthetic_array", "seed",
                   "max_number_of_paths", "samples_per_src", "max_num_paths_per_src", "cache_expiry_jitter_fraction",
                   "channel_state_max_age_s", "endpoint_displacement_threshold_m", "readiness_lag_max_ms",
                   "readiness_consecutive_samples"},
        "coupling": {"state_max_age_s", "runtime_lag_max_ms"},
    }
    if (not isinstance(value, dict) or set(value) != {"schema_version", *keys}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("radio profile requires schema_version=1 and radio/sionna/coupling objects")
    if isinstance(value.get("sionna"), dict):
        value["sionna"].setdefault("receiver_batch_mode", "pairwise")
        value["sionna"].setdefault("execution_mode", "in_process")
        value["sionna"].setdefault("worker_count", 3)
    for section, fields in keys.items():
        if not isinstance(value[section], dict) or set(value[section]) != fields:
            raise ValueError(f"missing or unknown {section} profile fields")
    radio, rt, coupling = value["radio"], value["sionna"], value["coupling"]
    if type(rt["execution_mode"]) is not str or rt["execution_mode"] not in ("in_process", "worker_pool"):
        raise ValueError("execution_mode must be in_process or worker_pool")
    if type(rt["worker_count"]) is not int or not 1 <= rt["worker_count"] <= 3:
        raise ValueError("worker_count must be an integer in 1..3")
    if rt["execution_mode"] == "in_process" and rt["worker_count"] != 3:
        raise ValueError("worker_count is configurable only with execution_mode=worker_pool; in_process requires default 3")
    if rt["receiver_batch_mode"] not in ("pairwise", "receiver_batch"):
        raise ValueError("receiver_batch_mode must be pairwise or receiver_batch")
    if rt["receiver_batch_mode"] == "receiver_batch" and rt["synthetic_array"] is not True:
        raise ValueError("receiver_batch requires the configured iso 1x1 synthetic arrays")
    if (radio["backend"] != "wifi" or radio["standard"] != "802.11n"
            or type(radio["technology_specific_modem"]) is not bool or radio["technology_specific_modem"]
            or type(radio["shared_spectrum_channels"]) is not int or radio["shared_spectrum_channels"] != 1):
        raise ValueError("Sionna urban profile requires explicit native Wi-Fi reference with one shared channel")
    for field, selected in {"antenna": "isotropic_single_element_0_dbi", "non_unicast_mode": "ErpOfdmRate6Mbps",
                            "control_basic_rates": "native_ns3_80211n_2_4ghz_basic_set",
                            "neighbor_discovery_mode": "preconfigured_static_neighbors",
                            "calibration": "uncalibrated_standard_reference"}.items():
        if radio[field] != selected:
            raise ValueError(f"current native reference profile requires {field}={selected}")
    channel = radio["channel_number"]
    if (type(channel) is not int or not 1 <= channel <= 13
            or type(radio["channel_width_mhz"]) is not int or radio["channel_width_mhz"] != 20):
        raise ValueError("current native profile requires a 2.4 GHz channel 1..13 at 20 MHz")
    for section, fields in ((radio, ["carrier_hz", "tx_power_w"]),
                            (rt, ["channel_state_max_age_s", "endpoint_displacement_threshold_m", "readiness_lag_max_ms"]),
                            (coupling, ["state_max_age_s", "runtime_lag_max_ms"])):
        for field in fields:
            number = section[field]
            if type(number) not in (int, float) or not 0 < number <= sys.float_info.max:
                raise ValueError(f"{field} must be finite and positive")
    scene_frequency = scene["radio"]["frequency_hz"]
    if type(scene_frequency) not in (int, float) or not 0 < scene_frequency <= sys.float_info.max:
        raise ValueError("shared scene frequency must be finite and positive")
    if (not math.isclose(radio["carrier_hz"], scene_frequency, rel_tol=0, abs_tol=.5)
            or not math.isclose(radio["carrier_hz"], (2412 + 5 * (channel - 1)) * 1e6, rel_tol=0, abs_tol=.5)):
        raise ValueError("radio carrier, resolved Wi-Fi channel and shared scene frequency disagree")
    for section, fields in ((radio, ["legacy_aloha_phy_rate_bps"]),
                            (rt, ["max_depth", "max_number_of_paths", "samples_per_src", "max_num_paths_per_src",
                                  "readiness_consecutive_samples"])):
        for field in fields:
            if type(section[field]) is not int or not 1 <= section[field] <= 2**32 - 1:
                raise ValueError(f"{field} must be a positive native uint32 value")
    if type(rt["seed"]) is not int or not 0 <= rt["seed"] <= 2**31 - 1:
        raise ValueError("Sionna seed must be a nonnegative native int32")
    for field in ("los", "specular_reflection", "diffuse_reflection", "diffraction", "edge_diffraction", "refraction", "synthetic_array"):
        if type(rt[field]) is not bool:
            raise ValueError(f"{field} must be a boolean")
    jitter = rt["cache_expiry_jitter_fraction"]
    if type(jitter) not in (int, float) or not 0 <= jitter <= .9:
        raise ValueError("cache_expiry_jitter_fraction must be finite in 0..0.9")
    if rt["execution_mode"] == "worker_pool":
        if rt["receiver_batch_mode"] != "receiver_batch":
            raise ValueError("worker_pool requires receiver_batch_mode=receiver_batch")
        if rt["max_number_of_paths"] > 90:
            raise ValueError("worker_pool supports max_number_of_paths <=90 in the verified native cache")
        if jitter != 0:
            raise ValueError("worker_pool requires cache_expiry_jitter_fraction=0; prefetch warnings do not change expiry guards")
        if rt["channel_state_max_age_s"] > .5 or rt["endpoint_displacement_threshold_m"] > .5:
            raise ValueError("worker_pool cannot relax the verified cache age/displacement limits of 0.5 s/m")
    if coupling["state_max_age_s"] > .5 or coupling["runtime_lag_max_ms"] > 250:
        raise ValueError("profile cannot relax the existing pose freshness and realtime lag limits")
    for section, fields in ((radio, keys["radio"] - {"backend", "standard", "technology_specific_modem", "shared_spectrum_channels",
                                                   "channel_number", "channel_width_mhz", "carrier_hz", "tx_power_w", "legacy_aloha_phy_rate_bps"}),
                            (rt, ["solver_profile"])):
        if any(not isinstance(section[field], str) or not section[field] for field in fields):
            raise ValueError("native radio profile metadata and mode names must be nonempty strings")
    return value


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


def prepare(config: Path, output: Path, uav_count: int | None = None, *,
            propagation_profile="geometry_logdistance", radio_config: Path | None = None,
            ground_material=None, building_material=None, thickness_m=None):
    scenario = UrbanScenario.load(config, root=ROOT)
    data = scenario.raw
    ids = {str(i) for i in scenario.uav_ids}
    if uav_count is not None and (type(uav_count) is not int or uav_count != len(ids)):
        raise ValueError("requested UAV count must match the complete scene roster")
    output = output.resolve()
    output.relative_to(ROOT.resolve())
    if output == scenario.terrain_bundle_path:
        raise ValueError("run output cannot overwrite its input terrain bundle")
    if propagation_profile not in ("geometry_logdistance", "sionna"):
        raise ValueError("urban propagation must explicitly be geometry_logdistance or sionna")
    if scenario.terrain_bundle_path is not None and propagation_profile != "sionna":
        raise ValueError("terrain_bundle requires Sionna; flat geometry propagation cannot represent terrain")
    if propagation_profile == "sionna":
        if radio_config is None or ground_material is None or building_material is None or thickness_m is None:
            raise ValueError("Sionna requires an explicit radio config and ITU ground/building materials/thickness")
        radio_config = radio_config.resolve()
        radio_config.relative_to(ROOT.resolve())
        radio_profile = read_rt_radio_profile(radio_config, data)
        if scenario.terrain_manifest is not None:
            materials = scenario.terrain_manifest["materials"]
            if (ground_material != materials["ground_material"] or building_material != materials["building_material"]
                    or thickness_m != materials["thickness_m"]):
                raise ValueError("RT profile materials/thickness must match the shared terrain bundle")
    elif any(value is not None for value in (radio_config, ground_material, building_material, thickness_m)):
        raise ValueError("explicit RT radio/material arguments require propagation_profile=sionna")
    buildings = (data["buildings"] if scenario.terrain_manifest is None else
                 [dict(id=name, min=building["bounds_m"][0], max=building["bounds_m"][1])
                  for name, building in scenario.terrain_manifest["building_bounds"].items()])
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
    resolved = copy.deepcopy(data)
    if scenario.terrain_bundle_path is not None:
        frozen_bundle = output / "terrain"
        frozen_bundle.mkdir(exist_ok=False)
        for relative in [*scenario.terrain_manifest["files"], "scene-manifest.json"]:
            destination = frozen_bundle / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(scenario.terrain_bundle_path / relative, destination)
        resolved["terrain_bundle"] = frozen_bundle.relative_to(ROOT.resolve()).as_posix()
        resolved["buildings"] = buildings
        resolved["mission"]["world_ground_reference_z_by_uav_m"] = {
            str(i): scenario.ground_for(i) for i in scenario.uav_ids}
        resolved["mission"]["model_rest_clearance_m"] = scenario.model_rest_clearance_m
        resolved["mission"]["ground_reference_derivation"] = scenario.ground_reference_derivation
    mission_scene = output / "mission-scene.json"
    mission_scene.write_text(json.dumps(resolved, indent=2, allow_nan=False) + "\n")
    # Verify the frozen copy, including its hashes, before either consumer starts.
    UrbanScenario.load(mission_scene, root=ROOT)
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
    if scenario.terrain_bundle_path is None:
        box(world, "urban_ground", [-1000, -1000, -1], [1000, 1000, 0], ".28 .30 .32 1")
        for b in buildings:
            box(world, "urban_building_" + b["id"], b["min"], b["max"], ".55 .57 .62 1")
    else:
        for model in ET.parse(frozen_bundle / "world.sdf").getroot().findall("world/model"):
            # Keep the prepared shared geometry; this world supplies all flight
            # systems and the same ENU spherical coordinates as the mission.
            for uri in model.findall(".//mesh/uri"):
                uri.text = "terrain/" + uri.text
            world.append(model)
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
    # Pinned ArduCopter4.8 uses WP_SPD in m/s. A pre-takeoff
    # DO_CHANGE_SPEED is reset when Guided enters position control; set the
    # upstream persistent default rather than introducing a control law.
    speed = float(data["mission"]["speed_mps"])
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("positive native WP_SPD required")
    defaults = output / "urban_sitl.parm"
    defaults.write_text((ROOT / "network/config/town01_sitl.parm").read_text()
                        + f"\n# Pinned ArduCopter4.8 native waypoint speed, m/s\nWP_SPD {speed:.9g}\n")
    launch["base_simulation"]["sitl_defaults"] = defaults.resolve().relative_to(ROOT).as_posix()
    launch["base_simulation"]["sitl_home"] = ",".join(str(frame[key]) for key in
            ("origin_latitude_deg", "origin_longitude_deg", "origin_elevation_m", "spherical_heading_deg"))
    launch["scenario"]["name"] = data["scenario"]
    launch["scenario"]["description"] = f"Configured {len(ids)} UAVs sharing native radio and one common scene geometry."
    launch["scenario"]["map"] = dict(id=data["scenario"], kind="idealized_shared_building_boxes", customer_survey_asset=False,
                                      world_file=(output / "world.sdf").resolve().relative_to(ROOT).as_posix(), scene_xml="not_used_no_rt.xml",
                                      camera_fragment="network/ns3/runtime_live_cameras.sdf.inc",
                                      gazebo_models=["urban_ground"] + ["urban_building_" + b["id"] for b in buildings])
    if propagation_profile == "sionna":
        if scenario.terrain_bundle_path is None:
            from scripts.product.prepare_sionna_scene import prepare as prepare_rt
            scene = prepare_rt(config, output / "world.sdf", output / "rt", ground_material=ground_material,
                               building_material=building_material, thickness_m=thickness_m)
        else:
            scene = frozen_bundle / "scene.xml"
            launch["scenario"]["map"].update(kind="shared_triangle_terrain", gazebo_models=["terrain", "buildings"])
        launch["scenario"]["map"]["scene_xml"] = scene.resolve().relative_to(ROOT).as_posix()
        resolved_radio = output / "radio-resolved.yaml"
        resolved_radio.write_text(yaml.safe_dump(radio_profile, sort_keys=False), encoding="utf-8")
        launch["radio"]["config"] = resolved_radio.relative_to(ROOT).as_posix()
    launch["command_post"]["position_m"] = vector(data["gcs"])
    launch["robots"] = [robot for robot in launch["robots"] if str(robot["system_id"]) in ids]
    if {str(robot["system_id"]) for robot in launch["robots"]} != ids:
        raise ValueError("native launch template does not cover the scene roster")
    for robot in launch["robots"]:
        uid = str(robot["system_id"])
        robot["position"] = vector(data["spawns"][uid]) + [0., 0., 0.]
        robot["nominal_radio_position_m"] = vector(data["spawns"][uid])
    launch["flight"]["missions"] = {f"uav{i}": [dict(name=f"waypoint{k}", position_m=p, hold_s=data["mission"]["waypoint_dwell_s"])
                                                       for k, p in enumerate(data["routes"][i])] for i in sorted(ids)}
    launch["flight"]["takeoff_relative_altitude_m"] = {
        f"uav{i}": scenario.routes[i][0][2] - scenario.ground_for(i) for i in scenario.uav_ids}
    names = {robot["name"] for robot in launch["robots"]}
    for capture in launch.get("evidence", {}).get("screenshots", []):
        capture["required_projected_uavs"] = [name for name in capture.get("required_projected_uavs", []) if name in names]
    (output / "scenario.yaml").write_text(yaml.safe_dump(launch, sort_keys=False))
    (output / "geometry-source.json").write_text(json.dumps(dict(source=str(config.resolve()), sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                                                                config=data, note="generated scene; no measured flight or radio outcomes"), indent=2) + "\n")
    print(output / "scenario.yaml")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".external/urban_ns3")
    parser.add_argument("--uav-count", type=int, choices=range(1, 6), help="expected exact scene roster size")
    parser.add_argument("--propagation-profile", choices=("geometry_logdistance", "sionna"), default="geometry_logdistance")
    parser.add_argument("--radio-config", type=Path)
    parser.add_argument("--ground-material")
    parser.add_argument("--building-material")
    parser.add_argument("--thickness-m", type=float)
    args = parser.parse_args()
    prepare(args.config, args.output, args.uav_count, propagation_profile=args.propagation_profile,
            radio_config=args.radio_config, ground_material=args.ground_material,
            building_material=args.building_material, thickness_m=args.thickness_m)
