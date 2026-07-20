#!/usr/bin/env python3
"""Render a 2x2 Gazebo/Sionna/LiDAR/telemetry MP4 from run artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
import numpy as np  # noqa: E402


ROOT_DIR = Path(__file__).resolve().parents[2]
FRAME_SIZE = (16.0, 9.0)
FRAME_DPI = 120
VIDEO_RESOLUTION = (int(FRAME_SIZE[0] * FRAME_DPI), int(FRAME_SIZE[1] * FRAME_DPI))
NUMERIC_RADIO_FIELDS = {
    "time_s",
    "elapsed_s",
    "tx_x",
    "tx_y",
    "tx_z",
    "rx_x",
    "rx_y",
    "rx_z",
    "snr_db",
    "sinr_db",
    "rssi_dbm",
    "rx_power_dbm",
    "pathloss_db",
    "js_db",
    "per_input",
    "service_tier_bps",
}


@dataclass(frozen=True)
class Obstacle:
    name: str
    bounds: tuple[float, float, float, float, float, float]
    kind: str
    source: str


@dataclass
class NodeSample:
    rel_s: float
    nodes: dict[str, dict[str, Any]]
    emitters: list[dict[str, Any]]
    source: str
    missing_nodes: list[str]
    stale_nodes: list[str]


@dataclass
class RadioGroup:
    rel_s: float
    rows: list[dict[str, Any]]


@dataclass
class LidarProof:
    points: np.ndarray | None
    source_label: str
    detail: str
    warning: str | None = None


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index].rstrip()
    return line.rstrip()


def split_inline_list(value: str) -> list[str]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return [value]
    inner = value[1:-1].strip()
    if not inner:
        return []
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    for char in inner:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value == "":
        return None
    if value.startswith("[") and value.endswith("]"):
        return [parse_scalar(part) for part in split_inline_list(value)]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    try:
        if any(char in value for char in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_key_value(text: str) -> tuple[str, Any] | None:
    if ":" not in text:
        return None
    key, raw = text.split(":", 1)
    key = key.strip()
    if not key:
        return None
    return key, parse_scalar(raw)


def load_scenario(path: Path) -> dict[str, Any]:
    """Load the YAML subset used by the scenario configs without PyYAML."""
    scenario: dict[str, Any] = {
        "name": path.stem,
        "description": "",
        "map": {},
        "command_post": {},
        "robots": [],
        "radio": {},
        "traffic_classes": [],
    }
    top: str | None = None
    sub: str | None = None
    current_robot: dict[str, Any] | None = None

    for raw_line in read_text(path).splitlines():
        clean = strip_yaml_comment(raw_line)
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        text = clean.strip()

        if indent == 0:
            current_robot = None
            sub = None
            if text.endswith(":"):
                top = text[:-1].strip()
                continue
            parsed = parse_key_value(text)
            if parsed:
                top = parsed[0]
            continue

        if top == "scenario":
            parsed = parse_key_value(text)
            if indent == 2 and text.endswith(":"):
                sub = text[:-1].strip()
                continue
            if indent == 2:
                sub = None
                if parsed:
                    key, value = parsed
                    if key == "name":
                        scenario["name"] = str(value)
                    elif key == "description":
                        scenario["description"] = str(value)
                    else:
                        scenario[key] = value
                continue
            if sub == "map" and parsed:
                key, value = parsed
                scenario["map"][key] = value
            continue

        if top == "command_post":
            parsed = parse_key_value(text)
            if parsed:
                key, value = parsed
                scenario["command_post"][key] = value
            continue

        if top == "robots":
            if text.startswith("- "):
                current_robot = {}
                scenario["robots"].append(current_robot)
                rest = text[2:].strip()
                parsed = parse_key_value(rest)
                if parsed:
                    key, value = parsed
                    current_robot[key] = value
                continue
            if current_robot is not None:
                parsed = parse_key_value(text)
                if parsed:
                    key, value = parsed
                    current_robot[key] = value
            continue

        if top == "radio":
            parsed = parse_key_value(text)
            if parsed:
                key, value = parsed
                scenario["radio"][key] = value
            continue

        if top == "traffic_classes" and text.startswith("- "):
            scenario["traffic_classes"].append(parse_scalar(text[2:].strip()))

    return scenario


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def as_position(value: Any, fallback: list[float] | tuple[float, ...] | None = None) -> list[float] | None:
    source = value if value is not None else fallback
    if not isinstance(source, (list, tuple)) or len(source) < 3:
        return None
    parsed = [as_float(source[0]), as_float(source[1]), as_float(source[2])]
    if any(item is None for item in parsed):
        return None
    return [float(parsed[0]), float(parsed[1]), float(parsed[2])]


def scenario_nodes(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    command_post = scenario.get("command_post") or {}
    cp_position = as_position(command_post.get("position_m"))
    if cp_position:
        nodes[str(command_post.get("id") or "cp")] = {
            "id": str(command_post.get("id") or "cp"),
            "role": str(command_post.get("role") or "command_post"),
            "position_m": cp_position,
            "stale": False,
            "source_topic": "scenario:command_post",
        }
    for robot in scenario.get("robots", []):
        if not isinstance(robot, dict):
            continue
        position = as_position(robot.get("nominal_radio_position_m"), robot.get("position"))
        if not position:
            continue
        node_id = str(robot.get("name") or robot.get("id") or f"robot{len(nodes)}")
        nodes[node_id] = {
            "id": node_id,
            "role": str(robot.get("role") or "uav"),
            "position_m": position,
            "stale": False,
            "source_topic": "scenario:nominal_radio_position_m",
        }
    return nodes


def parse_pose(text: str | None) -> np.ndarray:
    values = [as_float(part, 0.0) or 0.0 for part in (text or "").split()]
    padded = (values + [0.0] * 6)[:6]
    return np.array(padded[:3], dtype=float)


def parse_size(text: str | None) -> np.ndarray | None:
    values = [as_float(part) for part in (text or "").split()]
    if len(values) < 3 or any(value is None for value in values[:3]):
        return None
    return np.array([float(values[0]), float(values[1]), float(values[2])], dtype=float)


def resolve_world_path(scenario_path: Path, scenario: dict[str, Any]) -> Path | None:
    world_file = str((scenario.get("map") or {}).get("world_file") or "")
    if not world_file:
        return None
    candidates = [
        scenario_path.parent / world_file,
        ROOT_DIR / world_file,
        ROOT_DIR / "src/multiagent_simulation/worlds" / world_file,
        ROOT_DIR / "network/scenes" / world_file,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def obj_group_bounds(path: Path) -> list[Obstacle]:
    groups: list[tuple[str, list[list[float]]]] = []
    current_name = path.stem
    current_vertices: list[list[float]] = []
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("o ") or stripped.startswith("g "):
            if current_vertices:
                groups.append((current_name, current_vertices))
            current_name = stripped.split(maxsplit=1)[1].strip() or path.stem
            current_vertices = []
            continue
        if stripped.startswith("v "):
            parts = stripped.split()
            if len(parts) >= 4:
                position = as_position(parts[1:4])
                if position:
                    current_vertices.append(position)
    if current_vertices:
        groups.append((current_name, current_vertices))

    obstacles: list[Obstacle] = []
    for name, vertices in groups:
        array = np.asarray(vertices, dtype=float)
        if array.size == 0:
            continue
        mins = array.min(axis=0)
        maxs = array.max(axis=0)
        height = float(maxs[2] - mins[2])
        area = float((maxs[0] - mins[0]) * (maxs[1] - mins[1]))
        if height < 2.0 or area <= 1.0:
            continue
        if "terrain" in path.stem.lower() and area > 1_000_000.0:
            continue
        kind = "rf_blocker" if "blocker" in name.lower() or "rock" in name.lower() else "building"
        obstacles.append(
            Obstacle(
                name=name,
                bounds=(float(mins[0]), float(maxs[0]), float(mins[1]), float(maxs[1]), float(mins[2]), float(maxs[2])),
                kind=kind,
                source=str(path),
            )
        )
    return obstacles


def load_world_obstacles(world_path: Path | None) -> tuple[list[Obstacle], list[str]]:
    if world_path is None or not world_path.is_file():
        return [], []

    sources = [str(world_path)]
    obstacles: list[Obstacle] = []
    try:
        root = ET.parse(world_path).getroot()
    except ET.ParseError:
        return [], sources

    for model in root.findall(".//model"):
        model_name = model.attrib.get("name", "model")
        if "ground" in model_name.lower():
            continue
        model_pose = parse_pose(model.findtext("pose"))
        for link in model.findall(".//link"):
            link_pose = parse_pose(link.findtext("pose"))
            for collision in link.findall("collision"):
                box = collision.find(".//box/size")
                size = parse_size(box.text if box is not None else None)
                if size is None or size[2] < 2.0:
                    continue
                center = model_pose + link_pose + parse_pose(collision.findtext("pose"))
                mins = center - size / 2.0
                maxs = center + size / 2.0
                obstacles.append(
                    Obstacle(
                        name=f"{model_name}:collision",
                        bounds=(float(mins[0]), float(maxs[0]), float(mins[1]), float(maxs[1]), float(mins[2]), float(maxs[2])),
                        kind="collision",
                        source=str(world_path),
                    )
                )
            for uri in link.findall(".//mesh/uri"):
                if uri.text is None:
                    continue
                mesh_path = (world_path.parent / uri.text.strip()).resolve()
                if not mesh_path.is_file() or mesh_path.suffix.lower() != ".obj":
                    continue
                mesh_obstacles = obj_group_bounds(mesh_path)
                obstacles.extend(mesh_obstacles)
                sources.append(str(mesh_path))

    deduped: list[Obstacle] = []
    seen: set[tuple[int, int, int, int, int, int]] = set()
    for obstacle in obstacles:
        key = tuple(int(round(value * 10.0)) for value in obstacle.bounds)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(obstacle)
    return deduped, sorted(set(sources))


def default_rock_obstacle(scenario: dict[str, Any]) -> list[Obstacle]:
    world_name = str((scenario.get("map") or {}).get("world_file") or "").lower()
    scenario_name = str(scenario.get("name") or "").lower()
    if "rock" not in world_name and "rock" not in scenario_name:
        return []
    return [
        Obstacle(
            name="radio_blocker:fallback_bounds",
            bounds=(170.0, 230.0, -140.0, 140.0, 0.0, 140.0),
            kind="rf_blocker",
            source="built-in rock_demo fallback from validated radio_blocker bounds",
        )
    ]


def load_node_states(path: Path, scenario: dict[str, Any]) -> tuple[list[NodeSample], list[str]]:
    warnings: list[str] = []
    samples: list[NodeSample] = []
    raw_times: list[float] = []
    has_elapsed = False

    if path.is_file():
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f"node_state line {line_number} is not valid JSON")
                    continue
                raw_time = as_float(obj.get("elapsed_s"))
                if raw_time is not None:
                    has_elapsed = True
                else:
                    raw_time = as_float(obj.get("time_s"))
                if raw_time is None:
                    raw_time = float(len(samples))
                raw_times.append(raw_time)
                nodes: dict[str, dict[str, Any]] = {}
                for node in obj.get("nodes", []):
                    if not isinstance(node, dict):
                        continue
                    position = as_position(node.get("position_m"))
                    node_id = str(node.get("id") or "")
                    if not position or not node_id:
                        continue
                    normalized = dict(node)
                    normalized["id"] = node_id
                    normalized["position_m"] = position
                    normalized["role"] = str(node.get("role") or "uav")
                    normalized["stale"] = bool(node.get("stale", False))
                    normalized["source_topic"] = str(node.get("source_topic") or "")
                    nodes[node_id] = normalized
                emitters = [emitter for emitter in obj.get("emitters", []) if isinstance(emitter, dict)]
                samples.append(
                    NodeSample(
                        rel_s=raw_time,
                        nodes=nodes,
                        emitters=emitters,
                        source=str(obj.get("source") or path.name),
                        missing_nodes=[str(item) for item in obj.get("missing_nodes", [])],
                        stale_nodes=[str(item) for item in obj.get("stale_nodes", [])],
                    )
                )

    if not samples:
        fallback_nodes = scenario_nodes(scenario)
        warnings.append(f"no usable node_state samples in {path}; using scenario positions")
        samples = [
            NodeSample(
                rel_s=0.0,
                nodes=fallback_nodes,
                emitters=[],
                source="scenario fallback",
                missing_nodes=[],
                stale_nodes=[],
            )
        ]
        raw_times = [0.0]
        has_elapsed = True

    origin = 0.0 if has_elapsed else min(raw_times)
    for sample, raw_time in zip(samples, raw_times):
        sample.rel_s = max(0.0, float(raw_time - origin))
    samples.sort(key=lambda item: item.rel_s)
    return samples, warnings


def normalize_radio_row(row: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if key in NUMERIC_RADIO_FIELDS:
            parsed = as_float(value)
            normalized[key] = parsed if parsed is not None else value
        else:
            normalized[key] = value
    return normalized


def load_radio_groups(path: Path) -> tuple[list[RadioGroup], dict[str, np.ndarray], list[str]]:
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    raw_times: list[float] = []
    if path.is_file():
        with path.open("r", newline="", encoding="utf-8", errors="replace") as stream:
            reader = csv.DictReader(stream)
            for index, raw_row in enumerate(reader):
                row = normalize_radio_row(raw_row)
                raw_time = as_float(row.get("elapsed_s"))
                if raw_time is None:
                    raw_time = as_float(row.get("time_s"))
                if raw_time is None:
                    raw_time = float(index)
                    warnings.append(f"radio row {index + 2} has no time field; using row index")
                raw_times.append(raw_time)
                rows.append(row)

    if not rows:
        warnings.append(f"no usable radio rows in {path}")
        return [], {"time": np.asarray([], dtype=float)}, warnings

    origin = min(raw_times)
    grouped: dict[float, list[dict[str, Any]]] = {}
    for row, raw_time in zip(rows, raw_times):
        rel_s = max(0.0, float(raw_time - origin))
        row["rel_s"] = rel_s
        grouped.setdefault(round(rel_s, 6), []).append(row)

    groups = [RadioGroup(rel_s=time_key, rows=group_rows) for time_key, group_rows in grouped.items()]
    groups.sort(key=lambda group: group.rel_s)
    series = radio_series(groups)
    return groups, series, warnings


def first_number(row: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = as_float(row.get(name))
        if value is not None:
            return value
    return None


def finite_values(rows: list[dict[str, Any]], names: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = first_number(row, names)
        if value is not None:
            values.append(value)
    return values


def radio_series(groups: list[RadioGroup]) -> dict[str, np.ndarray]:
    time_values: list[float] = []
    sinr_values: list[float] = []
    rssi_values: list[float] = []
    js_values: list[float] = []
    per_values: list[float] = []
    tier_values: list[float] = []
    for group in groups:
        rows = group.rows
        sinr = finite_values(rows, ("sinr_db", "snr_db"))
        rssi = finite_values(rows, ("rssi_dbm", "rx_power_dbm"))
        js = finite_values(rows, ("js_db",))
        per = finite_values(rows, ("per_input",))
        tier = finite_values(rows, ("service_tier_bps",))
        time_values.append(group.rel_s)
        sinr_values.append(min(sinr) if sinr else float("nan"))
        rssi_values.append(float(np.mean(rssi)) if rssi else float("nan"))
        js_values.append(max(js) if js else float("nan"))
        per_values.append(max(per) if per else float("nan"))
        tier_values.append(min(tier) if tier else float("nan"))
    return {
        "time": np.asarray(time_values, dtype=float),
        "sinr": np.asarray(sinr_values, dtype=float),
        "rssi": np.asarray(rssi_values, dtype=float),
        "js": np.asarray(js_values, dtype=float),
        "per": np.asarray(per_values, dtype=float),
        "tier": np.asarray(tier_values, dtype=float),
    }


def sample_at_time(samples: list[NodeSample], time_s: float) -> NodeSample:
    times = [sample.rel_s for sample in samples]
    index = int(np.searchsorted(times, time_s, side="right") - 1)
    index = max(0, min(index, len(samples) - 1))
    return samples[index]


def group_at_time(groups: list[RadioGroup], time_s: float) -> RadioGroup | None:
    if not groups:
        return None
    times = [group.rel_s for group in groups]
    index = int(np.searchsorted(times, time_s, side="right") - 1)
    index = max(0, min(index, len(groups) - 1))
    return groups[index]


def radio_source_label(groups: list[RadioGroup], path: Path) -> str:
    sources = sorted(
        {
            str(row.get("source"))
            for group in groups
            for row in group.rows
            if row.get("source") not in (None, "")
        }
    )
    if sources:
        return f"{path.name}: " + ", ".join(sources[:4])
    return path.name


def focus_radio_row(group: RadioGroup | None) -> dict[str, Any] | None:
    if group is None or not group.rows:
        return None

    def score(row: dict[str, Any]) -> tuple[int, float]:
        tx = str(row.get("tx") or "")
        rx = str(row.get("rx") or "")
        sinr = first_number(row, ("sinr_db", "snr_db"))
        non_cp = 0 if tx != "cp" and rx != "cp" else 1
        return (non_cp, sinr if sinr is not None else float("inf"))

    return min(group.rows, key=score)


def node_position(nodes: dict[str, dict[str, Any]], node_id: str | None) -> np.ndarray | None:
    if not node_id:
        return None
    node = nodes.get(node_id)
    if not node:
        return None
    position = as_position(node.get("position_m"))
    return None if position is None else np.asarray(position, dtype=float)


def row_endpoint(row: dict[str, Any], prefix: str, nodes: dict[str, dict[str, Any]]) -> np.ndarray | None:
    x = as_float(row.get(f"{prefix}_x"))
    y = as_float(row.get(f"{prefix}_y"))
    z = as_float(row.get(f"{prefix}_z"), 0.0)
    if x is not None and y is not None and z is not None:
        return np.asarray([x, y, z], dtype=float)
    return node_position(nodes, str(row.get(prefix) or ""))


def all_node_history(samples: list[NodeSample], time_s: float) -> dict[str, np.ndarray]:
    history: dict[str, list[list[float]]] = {}
    for sample in samples:
        if sample.rel_s > time_s:
            break
        for node_id, node in sample.nodes.items():
            position = as_position(node.get("position_m"))
            if position:
                history.setdefault(node_id, []).append(position)
    return {node_id: np.asarray(points, dtype=float) for node_id, points in history.items() if len(points) >= 2}


def compute_view_bounds(
    scenario: dict[str, Any],
    node_samples: list[NodeSample],
    obstacles: list[Obstacle],
    radio_groups: list[RadioGroup],
) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for sample in node_samples:
        for node in sample.nodes.values():
            position = as_position(node.get("position_m"))
            if position:
                xs.append(position[0])
                ys.append(position[1])
    for obstacle in obstacles:
        xmin, xmax, ymin, ymax, _, _ = obstacle.bounds
        xs.extend([xmin, xmax])
        ys.extend([ymin, ymax])
    for group in radio_groups:
        for row in group.rows:
            for prefix in ("tx", "rx"):
                x = as_float(row.get(f"{prefix}_x"))
                y = as_float(row.get(f"{prefix}_y"))
                if x is not None and y is not None:
                    xs.append(x)
                    ys.append(y)

    map_size = (scenario.get("map") or {}).get("size_m")
    if isinstance(map_size, list) and len(map_size) >= 2:
        width = as_float(map_size[0])
        height = as_float(map_size[1])
        if width and height and max(width, height) <= 3000.0:
            return (-width / 2.0, width / 2.0, -height / 2.0, height / 2.0)

    if not xs or not ys:
        return (-500.0, 500.0, -300.0, 300.0)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    margin = max(80.0, 0.12 * max(xmax - xmin, ymax - ymin, 1.0))
    return (xmin - margin, xmax + margin, ymin - margin, ymax + margin)


def set_panel_style(ax: Any) -> None:
    ax.set_facecolor("#f8fafc")
    ax.grid(True, color="#d7dde5", linewidth=0.6, alpha=0.8)
    for spine in ax.spines.values():
        spine.set_color("#94a3b8")
        spine.set_linewidth(0.8)


def draw_obstacles(ax: Any, obstacles: list[Obstacle], *, alpha: float = 0.86, labels: bool = True) -> None:
    for obstacle in obstacles:
        xmin, xmax, ymin, ymax, _, zmax = obstacle.bounds
        if obstacle.kind == "rf_blocker":
            face = "#8b5e34"
            edge = "#4b2e16"
        elif obstacle.kind == "collision":
            face = "#a16207"
            edge = "#713f12"
        else:
            face = "#9ca3af"
            edge = "#475569"
        ax.add_patch(
            Rectangle(
                (xmin, ymin),
                xmax - xmin,
                ymax - ymin,
                facecolor=face,
                edgecolor=edge,
                linewidth=1.0,
                alpha=alpha,
                zorder=2,
            )
        )
        if labels and obstacle.kind in {"rf_blocker", "collision"} and (xmax - xmin) * (ymax - ymin) > 500.0:
            ax.text(
                (xmin + xmax) / 2.0,
                (ymin + ymax) / 2.0,
                f"{obstacle.name}\n{zmax:.0f} m",
                ha="center",
                va="center",
                fontsize=7,
                color="#1f2937",
                zorder=4,
                clip_on=True,
            )


def draw_nodes(ax: Any, nodes: dict[str, dict[str, Any]]) -> None:
    for node_id, node in sorted(nodes.items()):
        position = as_position(node.get("position_m"))
        if not position:
            continue
        role = str(node.get("role") or "")
        stale = bool(node.get("stale", False))
        if role == "command_post" or node_id == "cp":
            marker = "s"
            color = "#1d4ed8"
            size = 56
        else:
            marker = "^"
            color = "#059669"
            size = 62
        face = "none" if stale else color
        ax.scatter(
            [position[0]],
            [position[1]],
            marker=marker,
            s=size,
            facecolors=face,
            edgecolors=color,
            linewidths=1.4,
            zorder=7,
        )
        ax.text(
            position[0] + 10.0,
            position[1] + 10.0,
            f"{node_id} z={position[2]:.0f}",
            fontsize=7.2,
            color="#0f172a",
            zorder=8,
            clip_on=True,
        )


def sinr_color(value: float | None) -> str:
    if value is None:
        return "#64748b"
    if value < 0.0:
        return "#dc2626"
    if value < 6.0:
        return "#f97316"
    if value < 15.0:
        return "#ca8a04"
    return "#0f766e"


def draw_link_rows(ax: Any, rows: list[dict[str, Any]], nodes: dict[str, dict[str, Any]], *, focus: dict[str, Any] | None) -> None:
    for row in rows[:48]:
        tx = row_endpoint(row, "tx", nodes)
        rx = row_endpoint(row, "rx", nodes)
        if tx is None or rx is None:
            continue
        value = first_number(row, ("sinr_db", "snr_db"))
        is_focus = row is focus
        ax.plot(
            [tx[0], rx[0]],
            [tx[1], rx[1]],
            color=sinr_color(value),
            linewidth=2.8 if is_focus else 1.0,
            alpha=0.95 if is_focus else 0.25,
            zorder=5 if is_focus else 3,
        )


def rect_intersection_segment(start: np.ndarray, end: np.ndarray, bounds: tuple[float, float, float, float]) -> bool:
    xmin, xmax, ymin, ymax = bounds
    dx = float(end[0] - start[0])
    dy = float(end[1] - start[1])
    p = [-dx, dx, -dy, dy]
    q = [float(start[0] - xmin), float(xmax - start[0]), float(start[1] - ymin), float(ymax - start[1])]
    u1 = 0.0
    u2 = 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-12:
            if qi < 0:
                return False
            continue
        ratio = qi / pi
        if pi < 0:
            u1 = max(u1, ratio)
        else:
            u2 = min(u2, ratio)
        if u1 > u2:
            return False
    return True


def ray_rect_distance(origin: np.ndarray, direction: np.ndarray, bounds: tuple[float, float, float, float]) -> float | None:
    xmin, xmax, ymin, ymax = bounds
    ox, oy = float(origin[0]), float(origin[1])
    dx, dy = float(direction[0]), float(direction[1])
    tmin = -float("inf")
    tmax = float("inf")
    for o, d, lo, hi in ((ox, dx, xmin, xmax), (oy, dy, ymin, ymax)):
        if abs(d) < 1e-12:
            if o < lo or o > hi:
                return None
            continue
        t1 = (lo - o) / d
        t2 = (hi - o) / d
        tmin = max(tmin, min(t1, t2))
        tmax = min(tmax, max(t1, t2))
    if tmax < max(0.0, tmin):
        return None
    return max(0.0, tmin)


def rf_field(
    bounds: tuple[float, float, float, float],
    tx_position: np.ndarray | None,
    obstacles: list[Obstacle],
) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    xmin, xmax, ymin, ymax = bounds
    x_grid = np.linspace(xmin, xmax, 96)
    y_grid = np.linspace(ymin, ymax, 72)
    xx, yy = np.meshgrid(x_grid, y_grid)
    if tx_position is None:
        tx_position = np.asarray([(xmin + xmax) / 2.0, (ymin + ymax) / 2.0, 0.0], dtype=float)
    distance = np.hypot(xx - tx_position[0], yy - tx_position[1]) + 1.0
    field = -20.0 * np.log10(distance)
    blocker_rects = [
        (ob.bounds[0], ob.bounds[1], ob.bounds[2], ob.bounds[3])
        for ob in obstacles
        if ob.kind in {"rf_blocker", "collision", "building"}
    ][:12]
    for y_index in range(field.shape[0]):
        for x_index in range(field.shape[1]):
            point = np.asarray([xx[y_index, x_index], yy[y_index, x_index]], dtype=float)
            blocked = sum(rect_intersection_segment(tx_position[:2], point, rect) for rect in blocker_rects)
            if blocked:
                field[y_index, x_index] -= 10.0 + 3.0 * min(blocked, 3)
    return field, (xmin, xmax, ymin, ymax)


def draw_gazebo_panel(
    ax: Any,
    scenario: dict[str, Any],
    node_samples: list[NodeSample],
    sample: NodeSample,
    obstacles: list[Obstacle],
    radio_group: RadioGroup | None,
    focus_row: dict[str, Any] | None,
    view_bounds: tuple[float, float, float, float],
    time_s: float,
    node_state_path: Path,
) -> None:
    set_panel_style(ax)
    xmin, xmax, ymin, ymax = view_bounds
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title("Gazebo world", loc="left", fontsize=12, fontweight="bold", color="#0f172a")
    ax.set_title(f"t={time_s:05.1f}s", loc="right", fontsize=10, color="#334155")
    ax.set_xlabel("East x (m)", fontsize=8)
    ax.set_ylabel("North y (m)", fontsize=8)
    ax.add_patch(
        Rectangle(
            (xmin, ymin),
            xmax - xmin,
            ymax - ymin,
            facecolor="#eef2e6",
            edgecolor="#cbd5bf",
            linewidth=0.8,
            zorder=0,
        )
    )
    draw_obstacles(ax, obstacles, alpha=0.78, labels=True)
    for node_id, history in all_node_history(node_samples, time_s).items():
        if len(history) < 2:
            continue
        ax.plot(history[:, 0], history[:, 1], color="#334155", linewidth=0.8, alpha=0.35, zorder=4)
        if node_id in sample.nodes:
            ax.scatter(history[-1:, 0], history[-1:, 1], color="#334155", s=8, alpha=0.35, zorder=4)
    if radio_group:
        draw_link_rows(ax, radio_group.rows, sample.nodes, focus=focus_row)
    draw_nodes(ax, sample.nodes)
    for emitter in sample.emitters:
        position = as_position(emitter.get("position_m"))
        if position:
            ax.scatter([position[0]], [position[1]], marker="*", s=90, color="#dc2626", zorder=9)
            ax.text(position[0] + 12.0, position[1] + 12.0, str(emitter.get("id") or "emitter"), fontsize=7.2, color="#7f1d1d")
    world_file = str((scenario.get("map") or {}).get("world_file") or "unknown world")
    footer = f"Geometry: {world_file} | Nodes: {node_state_path.name} ({sample.source})"
    ax.text(0.01, 0.015, footer, transform=ax.transAxes, fontsize=7.2, color="#475569", va="bottom")


def draw_rf_panel(
    ax: Any,
    sample: NodeSample,
    obstacles: list[Obstacle],
    radio_group: RadioGroup | None,
    focus_row: dict[str, Any] | None,
    view_bounds: tuple[float, float, float, float],
    radio_label: str,
) -> None:
    set_panel_style(ax)
    xmin, xmax, ymin, ymax = view_bounds
    tx_position = row_endpoint(focus_row, "tx", sample.nodes) if focus_row else None
    field, extent = rf_field(view_bounds, tx_position, obstacles)
    ax.imshow(field, extent=extent, origin="lower", cmap="viridis", alpha=0.68, zorder=0, aspect="auto")
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title("Sionna/RF world", loc="left", fontsize=12, fontweight="bold", color="#0f172a")
    draw_obstacles(ax, obstacles, alpha=0.6, labels=False)
    if radio_group:
        draw_link_rows(ax, radio_group.rows, sample.nodes, focus=focus_row)
    draw_nodes(ax, sample.nodes)
    ax.set_xlabel("East x (m)", fontsize=8)
    ax.set_ylabel("North y (m)", fontsize=8)
    metrics = current_metrics_text(focus_row)
    ax.text(
        0.01,
        0.985,
        metrics,
        transform=ax.transAxes,
        fontsize=8.2,
        color="#f8fafc",
        va="top",
        ha="left",
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "#0f172a", "edgecolor": "none", "alpha": 0.76},
    )
    ax.text(
        0.01,
        0.015,
        f"RF samples: {radio_label} | overlay: CSV metrics on shared geometry",
        transform=ax.transAxes,
        fontsize=7.2,
        color="#f8fafc",
        va="bottom",
        bbox={"boxstyle": "round,pad=0.22", "facecolor": "#0f172a", "edgecolor": "none", "alpha": 0.60},
    )


def current_metrics_text(row: dict[str, Any] | None) -> str:
    if row is None:
        return "No radio row at this timestamp"
    tx = str(row.get("tx") or "?")
    rx = str(row.get("rx") or "?")
    sinr = first_number(row, ("sinr_db", "snr_db"))
    rssi = first_number(row, ("rssi_dbm", "rx_power_dbm"))
    js = first_number(row, ("js_db",))
    per = first_number(row, ("per_input",))
    tier = first_number(row, ("service_tier_bps",))
    values = [f"{tx}->{rx}"]
    if sinr is not None:
        values.append(f"SINR/SNR {sinr:.1f} dB")
    if rssi is not None:
        values.append(f"RSSI {rssi:.1f} dBm")
    if js is not None:
        values.append(f"J/S {js:.1f} dB")
    if per is not None:
        values.append(f"PER {per:.3f}")
    if tier is not None:
        values.append(f"tier {tier / 1e6:.1f} Mbps")
    return " | ".join(values)


def load_lidar_points_from_json_obj(obj: Any) -> np.ndarray | None:
    candidates: list[Any] = []
    if isinstance(obj, list):
        candidates.append(obj)
    elif isinstance(obj, dict):
        for key in ("points", "xyz", "point_cloud", "pointcloud", "cloud"):
            if key in obj:
                candidates.append(obj[key])
    for candidate in candidates:
        points: list[list[float]] = []
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    position = as_position([item.get("x"), item.get("y"), item.get("z")])
                else:
                    position = as_position(item)
                if position:
                    points.append(position)
        if points:
            return np.asarray(points, dtype=float)
    return None


def load_lidar_points_from_csv(path: Path) -> np.ndarray | None:
    try:
        with path.open("r", newline="", encoding="utf-8", errors="replace") as stream:
            sample = stream.read(2048)
            stream.seek(0)
            has_header = any(name in sample.lower().splitlines()[0] for name in ("x", "y", "z")) if sample else False
            if has_header:
                reader = csv.DictReader(stream)
                points = []
                for row in reader:
                    position = as_position([row.get("x"), row.get("y"), row.get("z")])
                    if position:
                        points.append(position)
                if points:
                    return np.asarray(points, dtype=float)
    except OSError:
        return None

    for delimiter in (",", None):
        try:
            data = np.loadtxt(path, delimiter=delimiter, ndmin=2)
        except Exception:
            continue
        if data.ndim == 2 and data.shape[1] >= 3:
            return np.asarray(data[:, :3], dtype=float)
    return None


def candidate_lidar_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    candidates: list[Path] = []
    for suffix in ("*.npy", "*.npz", "*.csv", "*.txt", "*.json", "*.jsonl"):
        candidates.extend(sorted(path.glob(suffix)))
    return candidates


def load_lidar_proof(path: Path | None) -> LidarProof:
    if path is None:
        return LidarProof(
            points=None,
            source_label="geometry preview",
            detail="No lidar proof was provided; preview is generated from shared scene bounds.",
        )
    if not path.exists():
        return LidarProof(
            points=None,
            source_label="geometry preview",
            detail="Lidar proof path does not exist; preview is generated from shared scene bounds.",
            warning=f"lidar proof missing: {path}",
        )

    for candidate in candidate_lidar_files(path):
        suffix = candidate.suffix.lower()
        points: np.ndarray | None = None
        try:
            if suffix == ".npy":
                data = np.load(candidate)
                if data.ndim == 2 and data.shape[1] >= 3:
                    points = np.asarray(data[:, :3], dtype=float)
            elif suffix == ".npz":
                loaded = np.load(candidate)
                for name in loaded.files:
                    data = loaded[name]
                    if data.ndim == 2 and data.shape[1] >= 3:
                        points = np.asarray(data[:, :3], dtype=float)
                        break
            elif suffix in {".csv", ".txt"}:
                points = load_lidar_points_from_csv(candidate)
            elif suffix == ".json":
                points = load_lidar_points_from_json_obj(json.loads(read_text(candidate)))
            elif suffix == ".jsonl":
                for line in read_text(candidate).splitlines():
                    if not line.strip():
                        continue
                    parsed = load_lidar_points_from_json_obj(json.loads(line))
                    if parsed is not None and len(parsed):
                        points = parsed
                        break
        except Exception:
            points = None
        if points is not None and points.size:
            finite = np.isfinite(points).all(axis=1)
            points = points[finite]
            if len(points):
                return LidarProof(
                    points=points,
                    source_label="lidar proof",
                    detail=f"Parsed {len(points)} finite xyz points from real PointCloud2 CSV.",
                )

    return LidarProof(
        points=None,
        source_label="geometry preview",
        detail="No parseable xyz points found in lidar proof; preview is generated from shared scene bounds.",
        warning=f"lidar proof was present but not parseable: {path}",
    )


def choose_lidar_sensor(sample: NodeSample, focus_row: dict[str, Any] | None) -> tuple[str, np.ndarray]:
    if focus_row:
        for key in ("rx", "tx"):
            node_id = str(focus_row.get(key) or "")
            position = node_position(sample.nodes, node_id)
            if position is not None:
                return node_id, position
    for node_id, node in sorted(sample.nodes.items()):
        if str(node.get("role") or "") == "uav":
            position = node_position(sample.nodes, node_id)
            if position is not None:
                return node_id, position
    for node_id in sorted(sample.nodes):
        position = node_position(sample.nodes, node_id)
        if position is not None:
            return node_id, position
    return "origin", np.asarray([0.0, 0.0, 0.0], dtype=float)


def geometry_lidar_points(sensor: np.ndarray, obstacles: list[Obstacle], max_range: float) -> np.ndarray:
    points: list[list[float]] = []
    rects = [
        (ob.bounds[0], ob.bounds[1], ob.bounds[2], ob.bounds[3], ob.bounds[4], ob.bounds[5])
        for ob in obstacles
        if ob.kind in {"rf_blocker", "collision", "building"}
    ][:30]
    for ray_index, angle in enumerate(np.linspace(-math.pi, math.pi, 360, endpoint=False)):
        direction = np.asarray([math.cos(angle), math.sin(angle)], dtype=float)
        best_distance = max_range
        best_height = 0.0
        for xmin, xmax, ymin, ymax, zmin, zmax in rects:
            hit = ray_rect_distance(sensor[:2], direction, (xmin, xmax, ymin, ymax))
            if hit is not None and 0.0 <= hit < best_distance:
                best_distance = hit
                best_height = min(max(sensor[2], zmin), zmax)
        if best_distance < max_range:
            world_xy = sensor[:2] + direction * best_distance
            points.append([world_xy[0] - sensor[0], world_xy[1] - sensor[1], best_height - sensor[2]])
        elif ray_index % 6 == 0:
            world_xy = sensor[:2] + direction * max_range
            points.append([world_xy[0] - sensor[0], world_xy[1] - sensor[1], -sensor[2]])
    return np.asarray(points, dtype=float)


def draw_lidar_panel(
    ax: Any,
    lidar: LidarProof,
    sample: NodeSample,
    obstacles: list[Obstacle],
    focus_row: dict[str, Any] | None,
    view_bounds: tuple[float, float, float, float],
) -> None:
    set_panel_style(ax)
    ax.set_title("LiDAR / PointCloud2", loc="left", fontsize=12, fontweight="bold", color="#0f172a")
    if lidar.points is not None:
        points = lidar.points
        if len(points) > 50_000:
            rng = np.random.default_rng(7)
            points = points[rng.choice(len(points), 50_000, replace=False)]
        axis_names = ["x", "y", "z"]
        if points.shape[1] >= 3:
            spans = np.nanmax(points[:, :3], axis=0) - np.nanmin(points[:, :3], axis=0)
            axis_a, axis_b = max(((0, 1), (0, 2), (1, 2)), key=lambda pair: spans[pair[0]] + spans[pair[1]])
            colors = points[:, axis_b]
        else:
            axis_a, axis_b = 0, 1
            colors = np.zeros(len(points))
        ax.scatter(points[:, axis_a], points[:, axis_b], c=colors, cmap="magma", s=2.0, alpha=0.78, linewidths=0)
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel(f"{axis_names[axis_a]} (m)", fontsize=8)
        ax.set_ylabel(f"{axis_names[axis_b]} (m)", fontsize=8)
        ax.text(
            0.01,
            0.015,
            textwrap.fill(f"Source: {lidar.detail}", width=76),
            transform=ax.transAxes,
            fontsize=7.2,
            color="#334155",
            va="bottom",
        )
        return

    sensor_id, sensor_position = choose_lidar_sensor(sample, focus_row)
    xmin, xmax, ymin, ymax = view_bounds
    max_range = max(120.0, 0.34 * max(xmax - xmin, ymax - ymin))
    points = geometry_lidar_points(sensor_position, obstacles, max_range=max_range)
    if len(points):
        ranges = np.hypot(points[:, 0], points[:, 1])
        ax.scatter(points[:, 0], points[:, 1], c=ranges, cmap="plasma", s=5.0, alpha=0.82, linewidths=0, zorder=3)
    ax.scatter([0.0], [0.0], marker="^", s=90, color="#0891b2", edgecolors="#164e63", linewidths=1.0, zorder=5)
    for obstacle in obstacles[:18]:
        xmin_o, xmax_o, ymin_o, ymax_o, _, _ = obstacle.bounds
        ax.add_patch(
            Rectangle(
                (xmin_o - sensor_position[0], ymin_o - sensor_position[1]),
                xmax_o - xmin_o,
                ymax_o - ymin_o,
                facecolor="#78716c",
                edgecolor="#44403c",
                linewidth=0.7,
                alpha=0.24,
                zorder=2,
            )
        )
    ax.set_xlim(-max_range, max_range)
    ax.set_ylim(-max_range, max_range)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel(f"Local x from {sensor_id} (m)", fontsize=8)
    ax.set_ylabel("Local y (m)", fontsize=8)
    ax.text(
        0.01,
        0.015,
        textwrap.fill(f"Source: {lidar.detail} This is not a real PointCloud2 sample.", width=76),
        transform=ax.transAxes,
        fontsize=7.2,
        color="#334155",
        va="bottom",
    )


def collect_log_tail(run_dir: Path, limit: int = 7) -> list[str]:
    log_dir = run_dir / "logs"
    if not log_dir.is_dir():
        return [f"logs directory missing: {log_dir}"]
    preferred = [
        "manual_demo.log",
        "validation.log",
        "ns3.log",
        "ns3_sionna_rt_live.log",
        "sionna_provider.log",
        "position_tracker.log",
        "ros_gazebo_launch.log",
    ]
    lines: list[str] = []
    for name in preferred:
        path = log_dir / name
        if not path.is_file():
            continue
        tail = [line.strip() for line in read_text(path).splitlines() if line.strip()][-2:]
        for line in tail:
            lines.append(f"{name}: {line}")
        if len(lines) >= limit:
            break
    if not lines:
        existing = sorted(log_dir.glob("*.log"))
        if existing:
            lines.append(f"logs present: {', '.join(path.name for path in existing[:6])}")
        else:
            lines.append(f"no *.log files in {log_dir}")
    return lines[-limit:]


def format_value(value: float | None, suffix: str = "", precision: int = 1) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{precision}f}{suffix}"


def draw_telemetry_panel(
    ax: Any,
    series: dict[str, np.ndarray],
    time_s: float,
    duration_s: float,
    sample: NodeSample,
    radio_group: RadioGroup | None,
    focus_row: dict[str, Any] | None,
    log_tail: list[str],
    lidar: LidarProof,
    run_dir: Path,
) -> None:
    ax.set_facecolor("#f8fafc")
    for spine in ax.spines.values():
        spine.set_color("#94a3b8")
        spine.set_linewidth(0.8)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title("Telemetry/logs", loc="left", fontsize=12, fontweight="bold", color="#0f172a")

    plot_ax = ax.inset_axes([0.07, 0.52, 0.88, 0.38])
    plot_ax.set_facecolor("#ffffff")
    times = series.get("time", np.asarray([], dtype=float))
    sinr = series.get("sinr", np.asarray([], dtype=float))
    rssi = series.get("rssi", np.asarray([], dtype=float))
    if len(times):
        plot_ax.axhspan(-200, 6, color="#fee2e2", alpha=0.55, linewidth=0)
        plot_ax.plot(times, sinr, color="#0f766e", linewidth=1.8, label="min SINR/SNR")
        plot_ax.set_ylabel("SINR/SNR dB", fontsize=7, color="#0f766e")
        plot_ax.tick_params(axis="y", labelsize=7, colors="#0f766e")
        twin = plot_ax.twinx()
        twin.plot(times, rssi, color="#be123c", linewidth=1.2, alpha=0.9, label="mean RSSI")
        twin.set_ylabel("RSSI dBm", fontsize=7, color="#be123c")
        twin.tick_params(axis="y", labelsize=7, colors="#be123c")
        plot_ax.axvline(time_s, color="#111827", linewidth=1.0, linestyle="--")
        plot_ax.set_xlim(0.0, max(duration_s, float(np.nanmax(times)) if len(times) else duration_s))
        finite_sinr = sinr[np.isfinite(sinr)]
        if len(finite_sinr):
            ymin = min(-5.0, float(np.nanmin(finite_sinr)) - 3.0)
            ymax = max(20.0, float(np.nanmax(finite_sinr)) + 3.0)
            plot_ax.set_ylim(ymin, ymax)
        plot_ax.grid(True, color="#e2e8f0", linewidth=0.6)
    else:
        plot_ax.text(0.5, 0.5, "No radio time series", ha="center", va="center", color="#64748b")
    plot_ax.set_xlabel("time (s)", fontsize=7)
    plot_ax.tick_params(axis="x", labelsize=7)

    current_sinr = first_number(focus_row, ("sinr_db", "snr_db")) if focus_row else None
    current_rssi = first_number(focus_row, ("rssi_dbm", "rx_power_dbm")) if focus_row else None
    current_js = first_number(focus_row, ("js_db",)) if focus_row else None
    current_per = first_number(focus_row, ("per_input",)) if focus_row else None
    current_tier = first_number(focus_row, ("service_tier_bps",)) if focus_row else None
    focus_link = "n/a" if focus_row is None else f"{focus_row.get('tx', '?')}->{focus_row.get('rx', '?')}"
    stale_count = sum(1 for node in sample.nodes.values() if node.get("stale"))
    radio_rows = len(radio_group.rows) if radio_group else 0

    summary_lines = [
        f"run: {run_dir.name}",
        f"focus link: {focus_link} | rows at t: {radio_rows}",
        (
            "current: "
            f"SINR/SNR {format_value(current_sinr, ' dB')} | "
            f"RSSI {format_value(current_rssi, ' dBm')} | "
            f"J/S {format_value(current_js, ' dB')} | "
            f"PER {format_value(current_per, '', 3)} | "
            f"tier {format_value((current_tier or 0.0) / 1e6 if current_tier else None, ' Mbps')}"
        ),
        f"nodes: {len(sample.nodes)} | stale: {stale_count} | missing: {', '.join(sample.missing_nodes) or 'none'}",
        f"node source: {sample.source} | lidar: {lidar.source_label}",
        "log tail:",
    ]
    summary_lines.extend(log_tail)
    wrapped: list[str] = []
    for line in summary_lines:
        wrapped.extend(textwrap.wrap(line, width=92, subsequent_indent="  ") or [""])
    ax.text(
        0.07,
        0.44,
        "\n".join(wrapped[:13]),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=7.5,
        family="monospace",
        color="#0f172a",
    )


def render_frame(
    frame_path: Path,
    time_s: float,
    scenario: dict[str, Any],
    node_samples: list[NodeSample],
    radio_groups: list[RadioGroup],
    series: dict[str, np.ndarray],
    obstacles: list[Obstacle],
    lidar: LidarProof,
    view_bounds: tuple[float, float, float, float],
    radio_label: str,
    log_tail: list[str],
    run_dir: Path,
    node_state_path: Path,
    duration_s: float,
) -> None:
    sample = sample_at_time(node_samples, time_s)
    radio_group = group_at_time(radio_groups, time_s)
    focus_row = focus_radio_row(radio_group)

    fig, axes = plt.subplots(2, 2, figsize=FRAME_SIZE, dpi=FRAME_DPI)
    fig.patch.set_facecolor("#e5e7eb")
    draw_gazebo_panel(
        axes[0, 0],
        scenario,
        node_samples,
        sample,
        obstacles,
        radio_group,
        focus_row,
        view_bounds,
        time_s,
        node_state_path,
    )
    draw_rf_panel(axes[0, 1], sample, obstacles, radio_group, focus_row, view_bounds, radio_label)
    draw_lidar_panel(axes[1, 0], lidar, sample, obstacles, focus_row, view_bounds)
    draw_telemetry_panel(
        axes[1, 1],
        series,
        time_s,
        duration_s,
        sample,
        radio_group,
        focus_row,
        log_tail,
        lidar,
        run_dir,
    )
    fig.suptitle(
        f"{scenario.get('name', 'radio demo')} composite radio evidence",
        x=0.012,
        y=0.992,
        ha="left",
        fontsize=13,
        fontweight="bold",
        color="#111827",
    )
    fig.text(
        0.988,
        0.992,
        "Panels are artifact visualizations; source labels identify real vs generated views.",
        ha="right",
        va="top",
        fontsize=8,
        color="#374151",
    )
    fig.subplots_adjust(left=0.045, right=0.985, top=0.93, bottom=0.055, wspace=0.11, hspace=0.20)
    fig.savefig(frame_path, dpi=FRAME_DPI, facecolor=fig.get_facecolor())
    plt.close(fig)


def encode_video(frames_dir: Path, frame_pattern: str, output_path: Path, fps: float) -> tuple[list[str], str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required but was not found in PATH")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_suffix(output_path.suffix + ".tmp.mp4")
    if temp_output.exists():
        temp_output.unlink()
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "warning",
        "-framerate",
        f"{fps:g}",
        "-i",
        str(frames_dir / frame_pattern),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with code {result.returncode}: {result.stderr.strip()}")
    temp_output.replace(output_path)
    return cmd, result.stderr.strip()


def path_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() and path.is_file() else None,
    }


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="Run artifact directory.")
    parser.add_argument("--scenario", required=True, help="Scenario YAML used for the run.")
    parser.add_argument("--radio-csv", required=True, help="Radio metric CSV, for example metrics/ns3_link_states.csv.")
    parser.add_argument("--node-state-jsonl", required=True, help="Node state JSONL artifact.")
    parser.add_argument("--output", required=True, help="Output MP4 path.")
    parser.add_argument("--duration", required=True, type=positive_float, help="Video duration in seconds.")
    parser.add_argument("--fps", required=True, type=positive_float, help="Video frame rate.")
    parser.add_argument("--lidar-proof", default=None, help="Optional xyz point proof file or directory.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_dir = resolve_path(args.run_dir)
    scenario_path = resolve_path(args.scenario)
    radio_csv_path = resolve_path(args.radio_csv)
    node_state_path = resolve_path(args.node_state_jsonl)
    output_path = resolve_path(args.output)
    lidar_path = resolve_path(args.lidar_proof) if args.lidar_proof else None

    for label, path in (
        ("run directory", run_dir),
        ("scenario", scenario_path),
        ("radio CSV", radio_csv_path),
        ("node_state JSONL", node_state_path),
    ):
        if label == "run directory":
            if not path.is_dir():
                raise SystemExit(f"{label} not found: {path}")
        elif not path.is_file():
            raise SystemExit(f"{label} not found: {path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    scenario = load_scenario(scenario_path)
    world_path = resolve_world_path(scenario_path, scenario)
    obstacles, geometry_sources = load_world_obstacles(world_path)
    if not obstacles:
        obstacles = default_rock_obstacle(scenario)
        if obstacles:
            warnings.append("world geometry could not be parsed; using rock_demo fallback obstacle bounds")
    node_samples, node_warnings = load_node_states(node_state_path, scenario)
    radio_groups, series, radio_warnings = load_radio_groups(radio_csv_path)
    lidar = load_lidar_proof(lidar_path)
    warnings.extend(node_warnings)
    warnings.extend(radio_warnings)
    if lidar.warning:
        warnings.append(lidar.warning)

    view_bounds = compute_view_bounds(scenario, node_samples, obstacles, radio_groups)
    radio_label = radio_source_label(radio_groups, radio_csv_path)
    log_tail = collect_log_tail(run_dir)
    frame_count = max(1, int(math.ceil(float(args.duration) * float(args.fps))))
    frame_pattern = "frame_%06d.png"

    with tempfile.TemporaryDirectory(prefix=f".{output_path.stem}_frames_", dir=str(output_path.parent)) as temp_name:
        frames_dir = Path(temp_name)
        for frame_index in range(frame_count):
            frame_time = min(float(args.duration), frame_index / float(args.fps))
            render_frame(
                frames_dir / (frame_pattern % frame_index),
                frame_time,
                scenario,
                node_samples,
                radio_groups,
                series,
                obstacles,
                lidar,
                view_bounds,
                radio_label,
                log_tail,
                run_dir,
                node_state_path,
                float(args.duration),
            )
            if frame_index == 0 or (frame_index + 1) % max(1, int(float(args.fps) * 5.0)) == 0 or frame_index + 1 == frame_count:
                print(f"rendered {frame_index + 1}/{frame_count} frames", flush=True)
        ffmpeg_cmd, ffmpeg_stderr = encode_video(frames_dir, frame_pattern, output_path, float(args.fps))

    manifest_path = output_path.parent / "video_manifest.json"
    manifest = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "scenario_name": scenario.get("name"),
        "duration_s": float(args.duration),
        "fps": float(args.fps),
        "frame_count": frame_count,
        "resolution": list(VIDEO_RESOLUTION),
        "output": path_info(output_path),
        "manifest": str(manifest_path),
        "inputs": {
            "scenario": path_info(scenario_path),
            "radio_csv": path_info(radio_csv_path),
            "node_state_jsonl": path_info(node_state_path),
            "lidar_proof": path_info(lidar_path) if lidar_path else None,
        },
        "panels": {
            "gazebo_world": {
                "geometry_sources": geometry_sources or [obstacle.source for obstacle in obstacles],
                "node_state_source": str(node_state_path),
                "description": "Top-down artifact view of shared Gazebo world geometry and node_state positions.",
            },
            "sionna_rf_world": {
                "radio_source": radio_label,
                "description": "RF overlay from radio CSV metrics drawn over the shared scene geometry.",
            },
            "lidar_pointcloud2": {
                "source": lidar.source_label,
                "detail": lidar.detail,
                "real_points": int(len(lidar.points)) if lidar.points is not None else 0,
            },
            "telemetry_logs": {
                "radio_series_samples": int(len(series.get("time", []))),
                "log_tail_sources": log_tail,
            },
        },
        "counts": {
            "node_state_samples": len(node_samples),
            "radio_time_groups": len(radio_groups),
            "obstacles": len(obstacles),
        },
        "view_bounds": list(view_bounds),
        "warnings": warnings,
        "ffmpeg": {
            "command": ffmpeg_cmd,
            "stderr": ffmpeg_stderr,
        },
    }
    write_manifest(manifest_path, manifest)
    print(json.dumps({"output": str(output_path), "manifest": str(manifest_path), "warnings": warnings}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
