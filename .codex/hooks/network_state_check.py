#!/usr/bin/env python3
"""Lightweight Codex hook for the long-running network/radio workflow."""

from __future__ import annotations

import argparse
from pathlib import Path


REQUIRED_STATE_FILES = [
    "doc/network_radio_integration_plan.md",
    "doc/network_radio_integration_plan_v2.md",
    "network/PROGRESS.md",
    "network/DECISIONS.md",
    "network/VALIDATION_REPORT.md",
    "network/NEXT_TASK.md",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    root = Path.cwd()
    missing = [path for path in REQUIRED_STATE_FILES if not (root / path).exists()]

    if args.compact:
        print(
            "Network/radio reminder: after compaction, reread the authoritative v2 plan and "
            "network state files before continuing."
        )

    if missing:
        print("Network/radio state warning: missing files:")
        for path in missing:
            print(f"- {path}")
        print("Create or restore these before long-running autonomous work.")
        return 0

    print(
        "Network/radio state files present. Keep PROGRESS.md, "
        "VALIDATION_REPORT.md, DECISIONS.md, and NEXT_TASK.md current."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
