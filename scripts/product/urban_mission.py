"""Small route/frame adapter; all flight control remains in ArduPilot Guided.

The world-to-NED mapping is the declared iris ArduPilotPlugin transform
gazeboXYZToNED=(roll180,yaw90): N=worldY, E=worldX, D=-worldZ.
Ground reference comes from scene/model geometry, never per-run Gazebo poses.
Actual agreement of this transform must be checked by a physical axis pilot.
"""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path


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


@dataclasses.dataclass(frozen=True)
class UrbanScenario:
    raw: dict
    spawns: dict[int, tuple[float, float, float]]
    routes: dict[int, tuple[tuple[float, float, float], ...]]
    ground_reference_z_m: float
    telemetry_max_age_s: float
    speed_mps: float
    waypoint_dwell_s: float

    @classmethod
    def load(cls, path: Path):
        raw = json.loads(path.read_text())
        ids = set(range(1, 6))
        spawns = {int(i): point(p) for i, p in raw["spawns"].items()}
        routes = {int(i): tuple(point(p) for p in route) for i, route in raw["routes"].items()}
        if set(spawns) != ids or set(routes) != ids or any(not route for route in routes.values()):
            raise ValueError("urban scenario requires spawn and nonempty route for each of five UAVs")
        mission = raw["mission"]
        ground, age, speed, dwell = (float(mission[k]) for k in
                ("world_ground_reference_z_m", "telemetry_max_age_s", "speed_mps", "waypoint_dwell_s"))
        if not all(math.isfinite(x) for x in (ground, age, speed, dwell)):
            raise ValueError("urban mission values must be finite")
        if not 0 < age <= .5 or speed <= 0 or dwell <= 0:
            raise ValueError("urban age must be <=0.5 s; speed and dwell must be positive")
        if any(p[2] < ground + 2 for route in routes.values() for p in route):
            raise ValueError("urban flight waypoints must be at least 2 m above declared ground reference")
        # Horizontal frame is explicit and fixed, not inferred from a GPS heading.
        if mission.get("ned_axes", "N=y,E=x,D=-z") != "N=y,E=x,D=-z":
            raise ValueError("unsupported urban NED axes; run a new frame qualification")
        return cls(raw, spawns, routes, ground, age, speed, dwell)

    def frame(self, uav_id, local_ned_ground):
        spawn = self.spawns[uav_id]
        return NedFrame((spawn[0], spawn[1], self.ground_reference_z_m), point(local_ned_ground))
