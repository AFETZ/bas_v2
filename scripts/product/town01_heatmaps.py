#!/usr/bin/env python3
"""Generate compact baseline, jammer, and delta Town01 heatmaps via live Sionna."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider.provider import query_tcp  # noqa: E402


METRICS = ("rssi_dbm", "sinr_db", "js_db", "service_available")


def request_grid(
    *,
    points: int,
    extent: tuple[float, float, float, float],
    altitude_m: float,
    jammer: bool,
) -> tuple[dict[str, Any], list[tuple[int, int, float, float]]]:
    xmin, xmax, ymin, ymax = extent
    samples: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    coordinates: list[tuple[int, int, float, float]] = []
    for row in range(points):
        y = ymin + (ymax - ymin) * row / (points - 1)
        for column in range(points):
            x = xmin + (xmax - xmin) * column / (points - 1)
            node_id = f"grid_{row}_{column}"
            samples.append(
                {
                    "id": node_id,
                    "role": "heatmap_sample",
                    "position_m": [x, y, altitude_m],
                    "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "antenna": "omni",
                }
            )
            links.append({"tx": "cp", "rx": node_id, "traffic_class": "control"})
            coordinates.append((row, column, x, y))
    emitters = []
    if jammer:
        emitters.append(
            {
                "id": "town01_jammer",
                "position_m": [50.0, -20.0, 15.0],
                "center_hz": 2.4e9,
                "bandwidth_hz": 20e6,
                "power_dbm": 40.0,
                "duty_cycle": 1.0,
                "antenna": "omni",
            }
        )
    request = {
        "type": "link_query",
        "time_s": time.time(),
        "deadline_ms": 60000,
        "radio": {"carrier_hz": 2.4e9, "bandwidth_hz": 20e6, "tx_power_dbm": 33.0},
        "nodes": [
            {
                "id": "cp",
                "role": "command_post",
                "position_m": [5.0, 0.0, 2.0],
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "antenna": "omni",
            },
            *samples,
        ],
        "emitters": emitters,
        "links": links,
    }
    return request, coordinates


def matrices(response: dict[str, Any], points: int) -> dict[str, np.ndarray]:
    result = {name: np.zeros((points, points), dtype=float) for name in METRICS}
    for link in response.get("links", []):
        _prefix, row_text, column_text = str(link["rx"]).split("_")
        row = int(row_text)
        column = int(column_text)
        result["rssi_dbm"][row, column] = float(link["rssi_dbm"])
        result["sinr_db"][row, column] = float(link["sinr_db"])
        result["js_db"][row, column] = float(link["js_db"])
        result["service_available"][row, column] = 1.0 if int(link["service_tier_bps"]) > 0 else 0.0
    return result


def plot_matrix(
    path: Path,
    values: np.ndarray,
    extent: tuple[float, float, float, float],
    title: str,
    label: str,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(7, 4.5))
    image = axis.imshow(values, extent=extent, origin="lower", aspect="auto")
    axis.set_title(title)
    axis.set_xlabel("Town01 x (m)")
    axis.set_ylabel("Town01 y (m)")
    figure.colorbar(image, ax=axis, label=label)
    figure.tight_layout()
    figure.savefig(path, dpi=140)
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5090)
    parser.add_argument("--points", type=int, default=7)
    parser.add_argument("--altitude-m", type=float, default=20.0)
    args = parser.parse_args()
    if not 3 <= args.points <= 31:
        raise SystemExit("--points must be in 3..31")

    run_dir = args.run_dir.resolve()
    heatmap_dir = run_dir / "heatmaps"
    heatmap_dir.mkdir(parents=True, exist_ok=True)
    extent = (0.0, 100.0, -2.0, 4.0)
    responses: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, np.ndarray]] = {}
    coordinates: list[tuple[int, int, float, float]] = []
    for phase, jammer in (("baseline", False), ("jammer", True)):
        request, coordinates = request_grid(
            points=args.points,
            extent=extent,
            altitude_m=args.altitude_m,
            jammer=jammer,
        )
        response = query_tcp(args.host, args.port, request, timeout_s=90.0)
        if response.get("type") != "link_state" or len(response.get("links", [])) != args.points**2:
            raise SystemExit(f"incomplete {phase} heatmap response")
        responses[phase] = response
        values[phase] = matrices(response, args.points)
    values["delta"] = {
        metric: values["jammer"][metric] - values["baseline"][metric] for metric in METRICS
    }

    labels = {
        "rssi_dbm": "RSSI (dBm)",
        "sinr_db": "SINR (dB)",
        "js_db": "J/S (dB)",
        "service_available": "Service available (1/0)",
    }
    for phase in ("baseline", "jammer", "delta"):
        for metric in METRICS:
            plot_matrix(
                heatmap_dir / f"{phase}_{metric}.png",
                values[phase][metric],
                extent,
                f"Town01 {phase}: {labels[metric]}",
                labels[metric],
            )

    csv_path = run_dir / "metrics/heatmap_samples.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["phase", "row", "column", "x_m", "y_m", *METRICS],
        )
        writer.writeheader()
        for phase in ("baseline", "jammer", "delta"):
            for row, column, x, y in coordinates:
                writer.writerow(
                    {
                        "phase": phase,
                        "row": row,
                        "column": column,
                        "x_m": x,
                        "y_m": y,
                        **{metric: values[phase][metric][row, column] for metric in METRICS},
                    }
                )
    summary = {
        "scene_id": responses["baseline"].get("scene_id"),
        "grid_points": args.points,
        "altitude_m": args.altitude_m,
        "extent_m": list(extent),
        "baseline_provider_latency_ms": responses["baseline"].get("provider_latency_ms"),
        "jammer_provider_latency_ms": responses["jammer"].get("provider_latency_ms"),
        "jammer": {
            "position_m": [50.0, -20.0, 15.0],
            "power_dbm": 40.0,
            "center_hz": 2.4e9,
            "bandwidth_hz": 20e6,
        },
        "baseline_min_sinr_db": float(np.min(values["baseline"]["sinr_db"])),
        "jammer_min_sinr_db": float(np.min(values["jammer"]["sinr_db"])),
        "max_sinr_degradation_db": float(-np.min(values["delta"]["sinr_db"])),
        "images": sorted(path.name for path in heatmap_dir.glob("*.png")),
    }
    (heatmap_dir / "heatmap_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
