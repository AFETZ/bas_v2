#!/usr/bin/env python3
"""Add fixed live Gazebo cameras and the command-post marker to a run-local world."""

from __future__ import annotations

import argparse
import copy
import xml.etree.ElementTree as ET
from pathlib import Path

TOUR_CAMERAS = {
    "customer_wide": {"pose": [0, 0, 7800, 0, 1.57079632679, 0], "horizontal_fov": 1.4, "far": 18000},
    "tower": {"pose": [1120, -920, 100, 0, .4, .983], "horizontal_fov": 1.1, "far": 3000},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--video-fps", type=int, default=0)
    parser.add_argument("--tour", action="store_true")
    args = parser.parse_args()
    world = args.world.read_text(encoding="utf-8")
    fragment = args.fragment.read_text(encoding="utf-8")
    if args.video_fps:
        # Run-local visual sensors only: no collision or propagation changes.
        sensors = ET.fromstring("<fragment>" + fragment + "</fragment>")
        if args.tour:
            original = sensors.findall("model")[-1]
            for name, camera in TOUR_CAMERAS.items():
                model = copy.deepcopy(original)
                model.set("name", f"native_radio_{name}_camera")
                model.find("pose").text = " ".join(map(str, camera["pose"]))
                sensor = model.find(".//sensor")
                sensor.set("name", name)
                sensor.find("topic").text = f"/native_radio/{name}/image"
                sensor.find("camera/horizontal_fov").text = str(camera["horizontal_fov"])
                sensor.find("camera/clip/far").text = str(camera["far"])
                sensors.append(model)
        for sensor in sensors.findall(".//sensor[@type='camera']"):
            sensor.find("update_rate").text = str(args.video_fps)
            sensor.find("camera/image/width").text = "1280"
            sensor.find("camera/image/height").text = "720"
        fragment = "\n".join(ET.tostring(child, encoding="unicode") for child in sensors)
    marker = "</world>"
    if marker not in world:
        raise RuntimeError(f"SDF world closing tag is absent: {args.world}")
    args.output.write_text(world.replace(marker, fragment + "\n  " + marker, 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
