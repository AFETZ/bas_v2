#!/usr/bin/env python3
"""Add fixed live Gazebo cameras and the command-post marker to a run-local world."""

from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-cameras", action="store_true", help="Retain scene markers without rendering observation cameras")
    args = parser.parse_args()
    world = args.world.read_text(encoding="utf-8")
    fragment = args.fragment.read_text(encoding="utf-8")
    if args.no_cameras:
        models = ET.fromstring('<fragment>' + fragment + '</fragment>')
        fragment = '\n'.join(ET.tostring(model, encoding='unicode') for model in models
                             if model.find('.//sensor[@type="camera"]') is None)
    marker = "</world>"
    if marker not in world:
        raise RuntimeError(f"SDF world closing tag is absent: {args.world}")
    args.output.write_text(world.replace(marker, fragment + "\n  " + marker, 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
