#!/usr/bin/env python3
"""Add fixed live Gazebo cameras and the command-post marker to a run-local world."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    world = args.world.read_text(encoding="utf-8")
    fragment = args.fragment.read_text(encoding="utf-8")
    marker = "</world>"
    if marker not in world:
        raise RuntimeError(f"SDF world closing tag is absent: {args.world}")
    args.output.write_text(world.replace(marker, fragment + "\n  " + marker, 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
