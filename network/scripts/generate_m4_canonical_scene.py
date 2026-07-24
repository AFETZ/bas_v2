#!/usr/bin/env python3
"""Generate the deterministic, shared Gazebo/Sionna M4 scene bundle.

The generator intentionally uses only the Python standard library.  Every
generated runtime byte is deterministic and the independent scene validator
does not import this module.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "src/multiagent_simulation/worlds/m4_canonical"
BUNDLE_PATH = ROOT / "network/config/m4_canonical_scene_bundle.json"
ZERO_SHA256 = "0" * 64
FRAME_TRANSFORM_VERSION = "ams-m4-coordinate-frames-v1"
GEODETIC_ORIGIN = {
    "datum": "WGS84",
    "elevation_m": 0.0,
    "heading_deg": 0.0,
    "latitude_deg": -35.3632621,
    "longitude_deg": 149.1652374,
}
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

X_GRID = (-10000.0, -5000.0, 0.0, 5000.0, 10000.0)
Y_GRID = (-5000.0, -2500.0, 0.0, 2500.0, 5000.0)
Z_GRID = (
    (0.0, 20.0, 30.0, 20.0, 0.0),
    (20.0, 60.0, 120.0, 70.0, 25.0),
    (30.0, 100.0, 180.0, 110.0, 40.0),
    (15.0, 55.0, 130.0, 65.0, 20.0),
    (0.0, 15.0, 25.0, 10.0, 0.0),
)
M4_SITL_SPAWN_CLEARANCE_M = 0.25
M4_SITL_UAVS = (
    ("uav1", 0, 1, -7000.0, -2500.0, 300.0),
    ("uav2", 1, 2, -4000.0, -2000.0, 320.0),
    ("uav3", 2, 3, 0.0, -1500.0, 350.0),
    ("uav4", 3, 4, 4000.0, -1000.0, 350.0),
    ("uav5", 4, 5, 8000.0, -500.0, 400.0),
)


@dataclass(frozen=True)
class Building:
    building_id: str
    cluster_id: str
    height_class: str
    floors: int
    floor_height_m: float
    center_x_m: float
    center_y_m: float
    width_m: float
    depth_m: float

    @property
    def base_z_m(self) -> float:
        return round(terrain_z(self.center_x_m, self.center_y_m), 6)

    @property
    def height_m(self) -> float:
        return self.floors * self.floor_height_m

    @property
    def bounds(self) -> dict[str, list[float]]:
        return {
            "x": [self.center_x_m - self.width_m / 2, self.center_x_m + self.width_m / 2],
            "y": [self.center_y_m - self.depth_m / 2, self.center_y_m + self.depth_m / 2],
            "z": [self.base_z_m, self.base_z_m + self.height_m],
        }


BUILDINGS = (
    Building("west_low_03", "settlement_west", "low", 3, 4.0, -7600.0, 3000.0, 120.0, 100.0),
    Building("west_medium_08", "settlement_west", "medium", 8, 4.0, -7200.0, 3200.0, 140.0, 120.0),
    Building("west_highrise_13", "settlement_west", "high", 13, 4.0, -6800.0, 3400.0, 160.0, 140.0),
    Building("east_low_04", "settlement_east", "low", 4, 4.0, 5000.0, -4300.0, 120.0, 100.0),
    Building("east_highrise_15", "settlement_east", "high", 15, 4.0, 5500.0, -4000.0, 140.0, 140.0),
    Building("east_medium_09", "settlement_east", "medium", 9, 4.0, 6000.0, -3700.0, 150.0, 120.0),
)

LANDMARKS = (
    ("west_low", (-9500.0, -4500.0, 25.0)),
    ("west_mid", (-9000.0, 0.0, 100.0)),
    ("west_high", (-8000.0, 4500.0, 170.0)),
    ("east_low", (9500.0, -4500.0, 25.0)),
    ("east_mid", (9000.0, 0.0, 110.0)),
    ("east_high", (8000.0, 4500.0, 170.0)),
)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def pretty_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def terrain_z(x_m: float, y_m: float) -> float:
    """Return the exact height on the triangulated terrain surface."""
    if not (X_GRID[0] <= x_m <= X_GRID[-1] and Y_GRID[0] <= y_m <= Y_GRID[-1]):
        raise ValueError("terrain query outside canonical bounds")
    xi = min(len(X_GRID) - 2, max(0, next((i for i in range(len(X_GRID) - 1) if x_m <= X_GRID[i + 1]), len(X_GRID) - 2)))
    yi = min(len(Y_GRID) - 2, max(0, next((i for i in range(len(Y_GRID) - 1) if y_m <= Y_GRID[i + 1]), len(Y_GRID) - 2)))
    u = (x_m - X_GRID[xi]) / (X_GRID[xi + 1] - X_GRID[xi])
    v = (y_m - Y_GRID[yi]) / (Y_GRID[yi + 1] - Y_GRID[yi])
    z00 = Z_GRID[yi][xi]
    z10 = Z_GRID[yi][xi + 1]
    z01 = Z_GRID[yi + 1][xi]
    z11 = Z_GRID[yi + 1][xi + 1]
    if v <= u:
        return z00 + u * (z10 - z00) + v * (z11 - z10)
    return z00 + u * (z11 - z01) + v * (z01 - z00)


def terrain_obj() -> bytes:
    lines = ["# AMS M4 canonical 20 km x 10 km triangulated terrain", "o canonical_terrain"]
    for yi, y_m in enumerate(Y_GRID):
        for xi, x_m in enumerate(X_GRID):
            lines.append(f"v {x_m:.6f} {y_m:.6f} {Z_GRID[yi][xi]:.6f}")
    for _ in range(len(X_GRID) * len(Y_GRID)):
        lines.append("vn 0.000000 0.000000 1.000000")
    width = len(X_GRID)
    for yi in range(len(Y_GRID) - 1):
        for xi in range(len(X_GRID) - 1):
            v00 = yi * width + xi + 1
            v10 = v00 + 1
            v01 = v00 + width
            v11 = v01 + 1
            lines.append(f"f {v00}//{v00} {v10}//{v10} {v11}//{v11}")
            lines.append(f"f {v00}//{v00} {v11}//{v11} {v01}//{v01}")
    return ("\n".join(lines) + "\n").encode()


def append_box(lines: list[str], vertices: list[tuple[float, float, float]], name: str, bounds: dict[str, list[float]]) -> None:
    base = len(vertices) + 1
    x0, x1 = bounds["x"]
    y0, y1 = bounds["y"]
    z0, z1 = bounds["z"]
    box = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    vertices.extend(box)
    lines.append(f"g {name}")
    for vertex in box:
        lines.append("v " + " ".join(f"{value:.6f}" for value in vertex))
    inverse = 1.0 / (3.0**0.5)
    for sx, sy, sz in (
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
    ):
        lines.append(
            f"vn {sx * inverse:.9f} {sy * inverse:.9f} {sz * inverse:.9f}"
        )
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    for a, b, c in faces:
        lines.append(
            f"f {base + a}//{base + a} {base + b}//{base + b} "
            f"{base + c}//{base + c}"
        )


def buildings_obj() -> bytes:
    lines = ["# AMS M4 canonical settlement geometry", "o canonical_buildings"]
    vertices: list[tuple[float, float, float]] = []
    for building in BUILDINGS:
        append_box(lines, vertices, building.building_id, building.bounds)
    return ("\n".join(lines) + "\n").encode()


def landmarks_obj() -> bytes:
    lines = ["# Six shared Gazebo/Sionna alignment landmarks", "o canonical_landmarks"]
    vertices: list[tuple[float, float, float]] = []
    for landmark_id, (x_m, y_m, z_m) in LANDMARKS:
        half = 2.0
        bounds = {"x": [x_m - half, x_m + half], "y": [y_m - half, y_m + half], "z": [z_m - half, z_m + half]}
        append_box(lines, vertices, f"landmark_{landmark_id}", bounds)
    return ("\n".join(lines) + "\n").encode()


def gazebo_world() -> bytes:
    # ArduPilot's enabled pre-arm checks require a gyro backend rate of at
    # least 1.8 times its 400 Hz scheduler rate.  Keep a modest margin above
    # the resulting 720 Hz floor without disabling any flight safety checks.
    # The locked Gazebo Sim runtime selects DART; its Bullet collision detector
    # and PGS constraint solver avoid the Dantzig/ODE LCP abort seen during a
    # simultaneous five-UAV take-off while retaining DART multirotor force and
    # joint support.
    return b'''<?xml version="1.0" ?>
<sdf version="1.9">
  <world name="map">
    <physics name="m4_capacity_physics" type="dart">
      <max_step_size>0.00125</max_step_size>
      <real_time_factor>1.0</real_time_factor>
      <real_time_update_rate>800</real_time_update_rate>
      <dart>
        <collision_detector>bullet</collision_detector>
        <solver><solver_type>pgs</solver_type></solver>
      </dart>
    </physics>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure"/>
    <plugin filename="gz-sim-air-speed-system" name="gz::sim::systems::AirSpeed"/>
    <plugin filename="gz-sim-altimeter-system" name="gz::sim::systems::Altimeter"/>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
    <plugin filename="gz-sim-magnetometer-system" name="gz::sim::systems::Magnetometer"/>
    <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat"/>
    <scene><ambient>0.45 0.45 0.45</ambient><sky>true</sky></scene>
    <spherical_coordinates>
      <latitude_deg>-35.3632621</latitude_deg><longitude_deg>149.1652374</longitude_deg>
      <elevation>0.0</elevation><heading_deg>0</heading_deg><surface_model>EARTH_WGS84</surface_model>
    </spherical_coordinates>
    <light type="directional" name="sun"><cast_shadows>true</cast_shadows><pose>0 0 1000 0 0 0</pose><direction>-0.4 0.2 -0.9</direction></light>
    <model name="canonical_terrain"><static>true</static><link name="link">
      <collision name="collision"><geometry><mesh><uri>terrain.obj</uri></mesh></geometry></collision>
      <visual name="visual"><geometry><mesh><uri>terrain.obj</uri></mesh></geometry><material><ambient>0.25 0.32 0.22 1</ambient><diffuse>0.31 0.42 0.27 1</diffuse></material></visual>
    </link></model>
    <model name="canonical_buildings"><static>true</static><link name="link">
      <collision name="collision"><geometry><mesh><uri>buildings.obj</uri></mesh></geometry></collision>
      <visual name="visual"><geometry><mesh><uri>buildings.obj</uri></mesh></geometry><material><ambient>0.38 0.36 0.34 1</ambient><diffuse>0.52 0.49 0.45 1</diffuse></material></visual>
    </link></model>
    <model name="canonical_landmarks"><static>true</static><link name="link">
      <collision name="collision"><geometry><mesh><uri>landmarks.obj</uri></mesh></geometry></collision>
      <visual name="visual"><geometry><mesh><uri>landmarks.obj</uri></mesh></geometry><material><ambient>0.9 0.15 0.1 1</ambient><diffuse>0.9 0.15 0.1 1</diffuse></material></visual>
    </link></model>
    <model name="cp"><static>true</static><pose>-8000 -2500 300 0 0 0</pose><link name="link">
      <visual name="visual"><geometry><sphere><radius>4</radius></sphere></geometry><material><ambient>0.1 0.2 0.9 1</ambient><diffuse>0.1 0.2 0.9 1</diffuse></material></visual>
    </link></model>
    <model name="jammer_m4"><static>true</static><pose>2000 -3000 100 0 0 0</pose><link name="link">
      <visual name="visual"><geometry><cylinder><radius>3</radius><length>10</length></cylinder></geometry><material><ambient>0.9 0.1 0.1 1</ambient><diffuse>0.9 0.1 0.1 1</diffuse></material></visual>
    </link></model>
  </world>
</sdf>
'''


def sionna_scene() -> bytes:
    return b'''<scene version="2.1.0">
  <bsdf type="itu-radio-material" id="terrain-ground"><string name="type" value="concrete"/><float name="thickness" value="1.0"/></bsdf>
  <bsdf type="itu-radio-material" id="settlement-concrete"><string name="type" value="concrete"/><float name="thickness" value="2.0"/></bsdf>
  <bsdf type="itu-radio-material" id="landmark-concrete"><string name="type" value="concrete"/><float name="thickness" value="0.5"/></bsdf>
  <shape type="obj" id="mesh-canonical-terrain"><string name="filename" value="terrain.obj"/><boolean name="face_normals" value="true"/><ref id="terrain-ground" name="bsdf"/></shape>
  <shape type="obj" id="mesh-canonical-buildings"><string name="filename" value="buildings.obj"/><boolean name="face_normals" value="true"/><ref id="settlement-concrete" name="bsdf"/></shape>
  <shape type="obj" id="mesh-canonical-landmarks"><string name="filename" value="landmarks.obj"/><boolean name="face_normals" value="true"/><ref id="landmark-concrete" name="bsdf"/></shape>
</scene>
'''


def scenario_robot_rows() -> str:
    """Render collision-clear SITL starts from the shared terrain source."""
    rows = []
    for name, instance, system_id, x_m, y_m, radio_z_m in M4_SITL_UAVS:
        spawn_z_m = terrain_z(x_m, y_m) + M4_SITL_SPAWN_CLEARANCE_M
        rows.append(
            "  - {name: %s, role: uav, instance: %d, system_id: %d, "
            "position: [%.1f, %.1f, %.2f, 0.0, 0.0, 0.0], "
            "nominal_radio_position_m: [%.1f, %.1f, %.1f], antenna: omni}"
            % (
                name,
                instance,
                system_id,
                x_m,
                y_m,
                spawn_z_m,
                x_m,
                y_m,
                radio_z_m,
            )
        )
    return "\n".join(rows)


def scenario_yaml() -> bytes:
    robot_rows = scenario_robot_rows()
    return ('''schema_version: 1
scenario:
  name: scenario_m4_canonical
  description: Final immutable kilometre-scale six-node/jammer M4-M8 scene.
  map:
    world_file: m4_canonical/m4_canonical.sdf
    size_m: [20000, 10000]
    terrain_height_variation_m: 180
    building_height_limit_floors: 15
base_simulation:
  launch_package: multiagent_simulation
  launch_file: multiagent_simulation.launch.py
  sitl_home: "-35.3632621,149.1652374,0,0"
  recommended_launch_arguments:
    gui: false
    rviz: false
    use_mapping_camera: false
    use_navigation_camera: false
    use_zed_camera: false
    robots_config_file: network/config/scenario_m4_canonical.yaml
command_post:
  id: cp
  role: command_post
  position_m: [-8000.0, -2500.0, 300.0]
  orientation_quat_xyzw: [0.0, 0.0, 0.0, 1.0]
  antenna: omni
robots:
{robot_rows}
radio:
  carrier_hz: 2400000000
  bandwidth_hz: 20000000
  tx_power_dbm_default: 33.0
  tx_power_w_range: [1.0, 2.0]
  service_tiers_file: network/config/service_tiers.yaml
traffic_classes: [control, payload, additional_data]
'''.replace("{robot_rows}", robot_rows).encode())


def radio_yaml() -> bytes:
    return b'''schema_version: 1
scenario: scenario_m4_canonical
radio:
  carrier_hz: 2400000000
  bandwidth_hz: 20000000
  tx_power_dbm: 33.0
  receiver_noise_figure_db: 6.0
  receiver_sensitivity_dbm: -105.0
  antenna: {tx_pattern: omni, rx_pattern: omni}
ns3:
  shared_medium_model: csma
  channel_rate_bps: 20000000
  channel_delay_ms: 2
  queue_max_packets: 100
  sionna_query_period_s: 1.0
  sionna_deadline_ms: 100
  require_sionna: true
traffic_offered_bps: {control: 1000, payload: 500000, additional_data: 100000}
priority_tos: {control: 184, payload: 40, additional_data: 0}
ipc: {protocol: tcp_json_lines, host: 127.0.0.1, port: 5090, log_file: logs/sionna_link_queries.jsonl}
sionna:
  required_for_acceptance: true
  scene:
    id: ams-m4-canonical-km-v2
    source: mitsuba_xml
    path: src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml
  solver:
    max_depth: 0
    samples_per_src: 1
    los: true
    specular_reflection: false
    diffuse_reflection: false
    refraction: false
    diffraction: false
    synthetic_array: true
    surface_epsilon_m: 0.05
    seed: 42
service_tier_selection:
  - {min_sinr_db: 20.0, service_tier_bps: 20000000, link_state: excellent, per_input: 0.001}
  - {min_sinr_db: 11.0, service_tier_bps: 2000000, link_state: good, per_input: 0.005}
  - {min_sinr_db: 6.0, service_tier_bps: 500000, link_state: usable, per_input: 0.02}
  - {min_sinr_db: 0.0, service_tier_bps: 100000, link_state: marginal, per_input: 0.08}
  - {min_sinr_db: -4.0, service_tier_bps: 10000, link_state: degraded, per_input: 0.25}
  - {min_sinr_db: -8.0, service_tier_bps: 1000, link_state: critical_only, per_input: 0.5}
  - {min_sinr_db: -999.0, service_tier_bps: 0, link_state: down, per_input: 1.0}
heatmaps:
  default_grid_points: 51
  altitude_m: 250.0
  extent_m: [-10000.0, 10000.0, -5000.0, 5000.0]
  degradation_sinr_db: 6.0
'''


def jammers_yaml() -> bytes:
    return b'''schema_version: 1
scenario: scenario_m4_canonical
jammers:
  - id: jammer_m4
    enabled: false
    role: jammer
    position_m: [2000.0, -3000.0, 100.0]
    orientation_quat_xyzw: [0.0, 0.0, 0.0, 1.0]
    center_hz: 2400000000
    bandwidth_hz: 20000000
    power_dbm: 40.0
    duty_cycle: 1.0
    antenna: omni
    time_behavior: runtime_off_on_off
'''


def material_manifest() -> bytes:
    return pretty_json({
        "contract": "ams.m4.scene-material-manifest/v1",
        "frequency_hz": 2400000000,
        "materials": [
            {"id": "terrain-ground", "itu_type": "concrete", "thickness_m": 1.0, "mesh": "terrain.obj"},
            {"id": "settlement-concrete", "itu_type": "concrete", "thickness_m": 2.0, "mesh": "buildings.obj"},
            {"id": "landmark-concrete", "itu_type": "concrete", "thickness_m": 0.5, "mesh": "landmarks.obj"},
        ],
        "schema_version": 1,
    })


def agl_csv(start_x: float, end_x: float, y_m: float, agl_m: float) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("distance_m", "x_m", "y_m", "terrain_z_m", "altitude_z_m", "agl_m"))
    samples = int(abs(end_x - start_x) / 25.0) + 1
    for index in range(samples):
        fraction = index / (samples - 1)
        x_m = start_x + (end_x - start_x) * fraction
        ground = terrain_z(x_m, y_m)
        writer.writerow((f"{index * 25.0:.6f}", f"{x_m:.6f}", f"{y_m:.6f}", f"{ground:.6f}", f"{ground + agl_m:.6f}", f"{agl_m:.6f}"))
    return stream.getvalue().encode()


def generated_assets() -> dict[str, bytes]:
    return {
        "src/multiagent_simulation/worlds/m4_canonical/terrain.obj": terrain_obj(),
        "src/multiagent_simulation/worlds/m4_canonical/buildings.obj": buildings_obj(),
        "src/multiagent_simulation/worlds/m4_canonical/landmarks.obj": landmarks_obj(),
        "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf": gazebo_world(),
        "src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml": sionna_scene(),
        "src/multiagent_simulation/worlds/m4_canonical/material_manifest.json": material_manifest(),
        "src/multiagent_simulation/worlds/m4_canonical/low_agl_path.csv": agl_csv(-9000.0, -8000.0, -4800.0, 30.0),
        "src/multiagent_simulation/worlds/m4_canonical/medium_agl_path.csv": agl_csv(8000.0, 9000.0, 4800.0, 80.0),
        "network/config/scenario_m4_canonical.yaml": scenario_yaml(),
        "network/config/radio_m4_canonical.yaml": radio_yaml(),
        "network/config/jammers_m4_canonical.yaml": jammers_yaml(),
    }


def asset_role(path: str) -> str:
    names = {
        "terrain.obj": "shared_collision_rf_terrain_mesh",
        "buildings.obj": "shared_collision_rf_building_mesh",
        "landmarks.obj": "shared_alignment_landmark_mesh",
        "m4_canonical.sdf": "gazebo_world",
        "sionna_scene.xml": "sionna_scene",
        "material_manifest.json": "radio_material_manifest",
        "low_agl_path.csv": "low_altitude_agl_samples",
        "medium_agl_path.csv": "medium_altitude_agl_samples",
        "scenario_m4_canonical.yaml": "six_node_flight_scenario",
        "radio_m4_canonical.yaml": "radio_provider_config",
        "jammers_m4_canonical.yaml": "jammer_config",
    }
    return names[Path(path).name]


def mesh_bounds(path: str) -> dict[str, list[float]] | None:
    if path.endswith("terrain.obj"):
        return {"x": [-10000.0, 10000.0], "y": [-5000.0, 5000.0], "z": [0.0, 180.0]}
    if path.endswith("buildings.obj"):
        all_bounds = [building.bounds for building in BUILDINGS]
        return {axis: [min(bounds[axis][0] for bounds in all_bounds), max(bounds[axis][1] for bounds in all_bounds)] for axis in ("x", "y", "z")}
    if path.endswith("landmarks.obj"):
        return {
            "x": [min(point[0] for _, point in LANDMARKS) - 2.0, max(point[0] for _, point in LANDMARKS) + 2.0],
            "y": [min(point[1] for _, point in LANDMARKS) - 2.0, max(point[1] for _, point in LANDMARKS) + 2.0],
            "z": [min(point[2] for _, point in LANDMARKS) - 2.0, max(point[2] for _, point in LANDMARKS) + 2.0],
        }
    return None


def pose_set(target: str, target_position: list[float], cp_position: list[float], control_position: list[float]) -> dict[str, Any]:
    nominal = {
        "cp": cp_position,
        "uav1": [-7000.0, -2500.0, 300.0],
        "uav2": [-4000.0, -2000.0, 320.0],
        "uav3": [0.0, -1500.0, 350.0],
        "uav4": [4000.0, -1000.0, 350.0],
        "uav5": control_position,
        "jammer_m4": [2000.0, -3000.0, 100.0],
    }
    nominal[target] = target_position
    return nominal


def coordinate_frame_contract() -> dict[str, Any]:
    """Return the exact Gazebo/ROS/ArduPilot/Sionna frame contract.

    ArduPilot LOCAL_POSITION_NED has a per-vehicle estimator origin, so its
    correspondence uses deltas from independently observed pre-arm baselines.
    GLOBAL_POSITION_INT is additionally checked as an absolute WGS84-to-
    Gazebo horizontal position because the scenario freezes SITL home to the
    exact Gazebo spherical origin.
    """

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
            "gazebo_world": {
                "frame_id": "world",
                **enu_common,
            },
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
            "sionna_scene": {
                "frame_id": "scene",
                **enu_common,
            },
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


def make_bundle(assets: dict[str, bytes]) -> dict[str, Any]:
    material_path = "src/multiagent_simulation/worlds/m4_canonical/material_manifest.json"
    asset_records = [
        {
            "mesh_bounds_m": mesh_bounds(path),
            "path": path,
            "role": asset_role(path),
            "sha256": sha256(payload),
            "size_bytes": len(payload),
        }
        for path, payload in sorted(assets.items())
    ]
    building_records = [
        {
            "bounds_m": building.bounds,
            "class": building.height_class,
            "floor_height_m": building.floor_height_m,
            "floors": building.floors,
            "height_m": building.height_m,
            "id": building.building_id,
        }
        for building in BUILDINGS
    ]
    clusters = []
    for cluster_id in ("settlement_west", "settlement_east"):
        members = [record for record in building_records if next(item for item in BUILDINGS if item.building_id == record["id"]).cluster_id == cluster_id]
        clusters.append({"id": cluster_id, "buildings": members, "required_classes": ["low", "medium", "high"]})

    bundle: dict[str, Any] = {
        "agl_paths": {
            "low": {"agl_bounds_m": [20.0, 50.0], "expected_agl_m": 30.0, "length_m": 1000.0, "path": "src/multiagent_simulation/worlds/m4_canonical/low_agl_path.csv", "sample_spacing_m": 25.0, "sha256": sha256(assets["src/multiagent_simulation/worlds/m4_canonical/low_agl_path.csv"])},
            "medium": {"agl_bounds_m": [50.0, 120.0], "expected_agl_m": 80.0, "length_m": 1000.0, "path": "src/multiagent_simulation/worlds/m4_canonical/medium_agl_path.csv", "sample_spacing_m": 25.0, "sha256": sha256(assets["src/multiagent_simulation/worlds/m4_canonical/medium_agl_path.csv"])},
        },
        "asset_manifest_sha256": sha256(canonical_json(asset_records)),
        "assets": asset_records,
        "building_clusters": clusters,
        "bundle_hash_policy": "sha256-canonical-json-with-bundle_sha256-zeroed/v1",
        "bundle_id": "ams-m4-canonical-km-v2",
        "bundle_sha256": ZERO_SHA256,
        "causal_scenarios": {
            "terrain_shadow": {
                "control_link": "cp>uav5",
                "target_link": "cp>uav1",
                "pose_sets": {
                    "terrain_good": pose_set("uav1", [-1500.0, -1000.0, 220.0], [-2500.0, 0.0, 175.0], [-2500.0, -1500.0, 250.0]),
                    "terrain_down": pose_set("uav1", [2500.0, 0.0, 175.0], [-2500.0, 0.0, 175.0], [-2500.0, -1500.0, 250.0]),
                    "terrain_recovery": pose_set("uav1", [-1500.0, -1000.0, 220.0], [-2500.0, 0.0, 175.0], [-2500.0, -1500.0, 250.0]),
                },
                "sequence": ["terrain_good", "terrain_down", "terrain_recovery"],
            },
            "building_blocked": {
                "control_link": "cp>uav5",
                "target_link": "cp>uav2",
                "pose_sets": {
                    "building_good": pose_set("uav2", [6000.0, -3400.0, 120.0], [5000.0, -4000.0, 80.0], [5000.0, -3000.0, 200.0]),
                    "building_down": pose_set("uav2", [6000.0, -4000.0, 80.0], [5000.0, -4000.0, 80.0], [5000.0, -3000.0, 200.0]),
                    "building_recovery": pose_set("uav2", [6000.0, -3400.0, 120.0], [5000.0, -4000.0, 80.0], [5000.0, -3000.0, 200.0]),
                },
                "sequence": ["building_good", "building_down", "building_recovery"],
            },
            "jammer_off_on_off": {
                "control_link": "cp>uav5",
                "target_link": "cp>uav3",
                "pose_set": pose_set("uav3", [4000.0, -3000.0, 250.0], [0.0, -3000.0, 250.0], [0.0, -2000.0, 300.0]),
                "sequence": ["off-1", "on", "off-2"],
            },
        },
        "contract": "ams.m4.canonical-scene-bundle/v2",
        "coordinate_frame": {
            "axes": "ENU",
            "gazebo_frame": "world",
            "origin": {"elevation_m": 0.0, "latitude_deg": -35.3632621, "longitude_deg": 149.1652374},
            "sionna_frame": "scene",
            "units": "m",
        },
        "frame_contract": coordinate_frame_contract(),
        "gazebo_to_sionna_transform_matrix": [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
        "gazebo_to_sionna_transform_version": "enu-identity-v1",
        "gazebo_world": "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf",
        "jammer_fixture": {"bandwidth_hz": 20000000, "center_hz": 2400000000, "duty_cycle": 1.0, "id": "jammer_m4", "position_m": [2000.0, -3000.0, 100.0], "power_dbm": 40.0},
        "landmarks": [
            {"expected_enu_m": list(point), "gazebo_sample_m": list(point), "id": landmark_id, "max_error_m": 1.0, "sionna_sample_m": list(point)}
            for landmark_id, point in LANDMARKS
        ],
        "nominal_pose_set": pose_set("uav5", [8000.0, -500.0, 400.0], [-8000.0, -2500.0, 300.0], [8000.0, -500.0, 400.0]),
        "operating_bounds_m": {
            "collision_usable": {"x": [-10000.0, 10000.0], "y": [-5000.0, 5000.0], "z": [0.0, 1000.0]},
            "rf_usable": {"x": [-10000.0, 10000.0], "y": [-5000.0, 5000.0], "z": [0.0, 1000.0]},
        },
        "range_fixtures": [
            {"distance_m": 1000.0, "geometry": "los", "id": "los_1km", "rx_position_m": [-9000.0, -4800.0, 220.0], "tx_position_m": [-10000.0, -4800.0, 220.0]},
            {"distance_m": 5000.0, "geometry": "los", "id": "los_5km", "rx_position_m": [-5000.0, -4800.0, 220.0], "tx_position_m": [-10000.0, -4800.0, 220.0]},
            {"distance_m": 10000.0, "geometry": "los", "id": "los_10km", "rx_position_m": [0.0, -4800.0, 220.0], "tx_position_m": [-10000.0, -4800.0, 220.0]},
            {"distance_m": 20000.0, "geometry": "los", "id": "los_20km", "rx_position_m": [10000.0, -4800.0, 220.0], "tx_position_m": [-10000.0, -4800.0, 220.0]},
            {"distance_m": 1000.0, "geometry": "building_blocked", "id": "building_blocked_1km", "rx_position_m": [6000.0, -4000.0, 80.0], "tx_position_m": [5000.0, -4000.0, 80.0]},
            {"distance_m": 5000.0, "geometry": "terrain_shadow", "id": "terrain_shadow_5km", "rx_position_m": [2500.0, 0.0, 175.0], "tx_position_m": [-2500.0, 0.0, 175.0]},
            {"distance_m": 10000.0, "geometry": "terrain_shadow", "id": "terrain_shadow_10km", "rx_position_m": [5000.0, 0.0, 160.0], "tx_position_m": [-5000.0, 0.0, 160.0]},
            {"distance_m": 20000.0, "geometry": "terrain_shadow", "id": "terrain_shadow_20km", "rx_position_m": [10000.0, 0.0, 100.0], "tx_position_m": [-10000.0, 0.0, 100.0]},
        ],
        "relief": {"delta_m": 180.0, "high_fixture": {"id": "terrain_high", "position_m": [0.0, 0.0, 180.0]}, "low_fixture": {"id": "terrain_low", "position_m": [-10000.0, -5000.0, 0.0]}},
        "scene_material_manifest_sha256": sha256(assets[material_path]),
        "schema_version": 2,
        "sionna_scene_xml": "src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml",
        "transitive_asset_policy": "all-local-regular-files-no-symlinks/v1",
    }
    bundle["bundle_sha256"] = sha256(canonical_json(bundle))
    return bundle


def desired_files() -> dict[Path, bytes]:
    assets = generated_assets()
    output = {ROOT / path: payload for path, payload in assets.items()}
    output[BUNDLE_PATH] = pretty_json(make_bundle(assets))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of rewriting when generated bytes differ")
    args = parser.parse_args()
    failures: list[str] = []
    for path, payload in desired_files().items():
        if args.check:
            try:
                actual = path.read_bytes()
            except OSError as exc:
                failures.append(f"{path.relative_to(ROOT)}: {exc}")
                continue
            if actual != payload:
                failures.append(f"{path.relative_to(ROOT)}: generated bytes differ")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: M4 canonical scene bytes are deterministic" if args.check else "PASS: generated M4 canonical scene")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
