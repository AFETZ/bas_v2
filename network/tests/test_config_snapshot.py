"""A snapshot preserves inputs and distinguishes them from effective read-back."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "config_snapshot", ROOT / "scripts/product/capture_effective_config.py"
)
SNAPSHOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SNAPSHOT)


def test_mutable_inputs_are_copied_and_coverage_does_not_invent_parameters(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    world = tmp_path / "world.sdf"
    world.write_text("<sdf>actual run world</sdf>")
    params = tmp_path / "sitl.parm"
    params.write_text("ARMING_CHECK 1\n")
    result = SNAPSHOT.capture_snapshot(run, stage="initial", repo_root=tmp_path,
        configs=[("world", world)], observed=[("ardupilot_uav1", params)],
        not_enabled=["sionna"], image_id="sha256:test-fixture")
    world.write_text("changed after capture")
    snapshot = run / "configuration/initial"
    assert (snapshot / result["coverage"]["world"]["file"]).read_text() == "<sdf>actual run world</sdf>"
    assert result["coverage"]["world"]["scope"] == "configured_input"
    assert result["coverage"]["ardupilot_uav1"]["scope"] == "effective_parameter_export"
    assert result["coverage"]["ardupilot_uav2"]["status"] == "not_recorded"
    assert result["coverage"]["sionna"]["status"] == "not_enabled"
    assert result["source"]["head"] is None
    assert json.loads((snapshot / "manifest.json").read_text())["container_image_id"] == "sha256:test-fixture"


def test_existing_snapshot_cannot_be_replaced(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    SNAPSHOT.capture_snapshot(run, stage="initial", repo_root=tmp_path, configs=[], observed=[])
    manifest = run / "configuration/initial/manifest.json"
    before = manifest.read_bytes()
    with pytest.raises(FileExistsError):
        SNAPSHOT.capture_snapshot(run, stage="initial", repo_root=tmp_path, configs=[], observed=[])
    assert manifest.read_bytes() == before


def test_host_launcher_metadata_is_explicit_fallback_when_git_is_unmounted(tmp_path, monkeypatch):
    monkeypatch.setenv("BAS_SOURCE_HEAD", "a" * 40)
    monkeypatch.setenv("BAS_SOURCE_DIRTY", "0")
    result = SNAPSHOT.source_state(tmp_path)
    assert result["head"] == "a" * 40
    assert result["dirty_count"] == 0
    assert result["full_git_snapshot_available"] is False
    assert "launcher env" in result["source"]
