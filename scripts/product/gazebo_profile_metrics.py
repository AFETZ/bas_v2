#!/usr/bin/env python3
"""Extract host-monotonic, profile-local Gazebo real-time-factor samples."""

from __future__ import annotations

import math
import re
import statistics
from pathlib import Path
from typing import Any


TIMESTAMPED_RTF = re.compile(
    r"^(?P<monotonic_ns>[0-9]+)\t.*real_time_factor:\s*"
    r"(?P<rtf>[0-9.eE+-]+)\s*$"
)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def profile_gazebo_rtf(path: Path, start_ns: int, end_ns: int) -> dict[str, Any]:
    values: list[float] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = TIMESTAMPED_RTF.match(line)
        if not match:
            continue
        observed_ns = int(match.group("monotonic_ns"))
        value = float(match.group("rtf"))
        if start_ns <= observed_ns <= end_ns and math.isfinite(value):
            values.append(value)
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p5": percentile(values, 0.05),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "scope": "Gazebo stats received inside the host-monotonic profile window",
        "source": str(path),
    }
