"""Focused static/data contracts for the selectable native five-UAV demo."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Callable

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "network/ns3/run_native_radio_five_uav.sh"
CAPTURE = ROOT / "scripts/product/capture_live_gazebo_screenshots.py"
SCENARIO_RUNNER = ROOT / "scripts/product/native_radio_five_uav_scenario.py"
SUMMARIZER = ROOT / "scripts/product/summarize_native_radio_five_uav.py"
STACK_HEALTH = ROOT / "scripts/product/town01_stack_health.py"
SCENARIOS = (
    ROOT / "network/config/scenario_5uav_town01_native_product.yaml",
    ROOT / "network/config/scenario_5uav_rock_demo_native_product.yaml",
)
UAVS = {f"uav{index}" for index in range(1, 6)}


def resolve_product_path(configured_path: object) -> Path:
    path = Path(str(configured_path))
    return path if path.is_absolute() else ROOT / path


@pytest.mark.parametrize("scenario_path", SCENARIOS, ids=("town01", "rock_demo"))
def test_five_uav_scenario_references_resolve(scenario_path: Path) -> None:
    config = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    scene_map = config["scenario"]["map"]
    references = {
        "world": scene_map["world_file"],
        "Sionna scene": scene_map["scene_xml"],
        "camera fragment": scene_map["camera_fragment"],
        "radio config": config["radio"]["config"],
        "SITL defaults": config["base_simulation"]["sitl_defaults"],
    }

    assert {robot["name"] for robot in config["robots"]} == UAVS
    assert scene_map["gazebo_models"]
    for label, configured_path in references.items():
        resolved = resolve_product_path(configured_path)
        assert resolved.is_file(), f"{scenario_path.name}: missing {label}: {resolved}"


def test_rugged_scenario_drives_all_five_uav_missions_and_observations() -> None:
    config = yaml.safe_load(SCENARIOS[1].read_text(encoding="utf-8"))
    flight = config["flight"]
    missions = flight["missions"]
    observations = flight["observations"]
    observation_names = {item["name"] for item in observations}

    assert set(missions) == UAVS
    assert set(flight["takeoff_relative_altitude_m"]) == UAVS
    assert observation_names == {
        "clear_observation",
        "shadow_observation",
        "recovery_observation",
    }
    assert all(item["probe_packets_per_uav"] > 0 for item in observations)
    for uav in UAVS:
        assert observation_names <= {waypoint["name"] for waypoint in missions[uav]}


def test_runner_selects_scenario_and_preserves_generic_runtime_contracts() -> None:
    runner = RUNNER.read_text(encoding="utf-8")

    assert 'SCENARIO_KEY="${BAS_NATIVE_FIVE_SCENARIO:-town01}"' in runner
    assert 'GUI="${BAS_NATIVE_FIVE_GUI:-0}"' in runner
    assert 'if [[ "$SCENARIO_KEY" == rock_demo ]]; then' in runner
    assert "scenario_5uav_rock_demo_native_product.yaml" in runner
    assert "scenario_${UAV_COUNT}uav_town01_native_product.yaml" in runner
    assert '-e BAS_NATIVE_FIVE_SCENARIO="$SCENARIO_KEY"' in runner
    assert '-e BAS_NATIVE_FIVE_GUI="$GUI"' in runner

    assert '--scenario-config "$SCENARIO"' in runner
    assert runner.count('--scenario-config="$SCENARIO"') >= 2
    assert 'summary_args=(--run-dir "$RUN_DIR" --scenario-config "$SCENARIO")' in runner

    assert 'gui_args=(-e DISPLAY="$DISPLAY"' in runner
    assert "GAZEBO_GUI=false" in runner
    assert "HEADLESS_RENDERING=true" in runner
    assert "GAZEBO_GUI=true" in runner
    assert "HEADLESS_RENDERING=false" in runner
    assert 'gui:="$GAZEBO_GUI"' in runner
    assert 'headless_rendering:="$HEADLESS_RENDERING"' in runner

    build_start = runner.index('if [[ "${BAS_NATIVE_FIVE_SKIP_BUILD:-0}" == 1 ]]')
    build_end = runner.index("mapfile -t DEPENDENCIES", build_start)
    build = runner[build_start:build_end]
    assert 'if [[ ! -f "$NS3_DIR/cmake-cache/CMakeCache.txt" ]]; then' in build
    assert "./ns3 configure --enable-examples --enable-tests --enable-python-bindings" in build
    assert "./ns3 build upstream-sionna-tap-spike" in build
    assert "rm -rf" not in build

    packet_outcome_cli = re.compile(r"^\s*--packet[-_]?outcome", re.IGNORECASE | re.MULTILINE)
    assert packet_outcome_cli.search(runner) is None


def isolated_capture_config_loader() -> Callable[[Path], dict[str, object]]:
    source = CAPTURE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CAPTURE))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "load_capture_config"
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace: dict[str, object] = {"Path": Path, "yaml": yaml}
    exec(compile(module, str(CAPTURE), "exec"), namespace)
    return namespace["load_capture_config"]  # type: ignore[return-value]


def test_capture_is_selected_scenario_driven_without_town_coordinate_constants() -> None:
    source = CAPTURE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CAPTURE))
    town_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "town01" in node.value.lower()
    ]

    assert town_literals == ["network/config/scenario_5uav_town01_native_product.yaml"]
    assert "load_capture_config(args.scenario_config)" in source
    assert 'parser.add_argument("--scenario-config"' in source

    rugged_yaml = yaml.safe_load(SCENARIOS[1].read_text(encoding="utf-8"))
    loaded = isolated_capture_config_loader()(SCENARIOS[1])
    expected_cameras = rugged_yaml["evidence"]["cameras"]
    expected_missions = rugged_yaml["flight"]["missions"]

    assert loaded["scenario_name"] == rugged_yaml["scenario"]["name"]
    assert loaded["map_id"] == rugged_yaml["scenario"]["map"]["id"]
    assert {
        name: camera["pose"] for name, camera in loaded["cameras"].items()
    } == {name: camera["pose"] for name, camera in expected_cameras.items()}
    for observation in ("clear_observation", "shadow_observation", "recovery_observation"):
        assert set(loaded["mission_targets"][observation]) == UAVS
        assert loaded["mission_targets"][observation] == {
            uav: next(
                waypoint["position_m"]
                for waypoint in mission
                if waypoint["name"] == observation
            )
            for uav, mission in expected_missions.items()
        }


def test_causal_probe_uses_real_endpoint_ack_and_shared_monotonic_clock() -> None:
    scenario_source = SCENARIO_RUNNER.read_text(encoding="utf-8")
    capture_source = CAPTURE.read_text(encoding="utf-8")
    summary_source = SUMMARIZER.read_text(encoding="utf-8")

    assert 'message.kind == "p2p_downlink"' in scenario_source
    assert '"p2p_downlink_ack"' in scenario_source
    assert "p2p.sendto(response, (GCS_IP, P2P_PORT))" in scenario_source
    assert '"application_retransmissions": False' in scenario_source

    assert "frame_received_monotonic_ns" in capture_source
    assert "frame_received_monotonic_ns < self.phase_seen_monotonic_ns" in capture_source
    assert 'metadata.get("frame_received_monotonic_ns")' in summary_source
    assert 'event.get("wall_monotonic_ns")' in summary_source
    assert "2_000_000_000" in summary_source


def test_stack_health_uses_the_selected_map_model_contract() -> None:
    source = STACK_HEALTH.read_text(encoding="utf-8")

    assert 'scene_map.get("gazebo_models", [])' in source
    assert "all(name in models for name in expected_world_models)" in source
    assert '"cavise_town01" in models' not in source
    rugged = yaml.safe_load(SCENARIOS[1].read_text(encoding="utf-8"))
    assert set(rugged["scenario"]["map"]["gazebo_models"]) == {
        "engineering_terrain_visual",
        "engineering_buildings_visual",
        "radio_blocker",
    }
