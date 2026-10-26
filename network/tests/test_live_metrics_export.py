"""Observed trajectory export: retain gaps and never invent clocks or targets."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "live_metrics_report", ROOT / "scripts/product/summarize_native_radio_five_uav.py"
)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def snapshot(at, z=None, *, stale=False, frame="ENU", source_time=True):
    node = {"id": "uav1", "position_m": [12, 25, z], "stale": stale}
    if source_time:
        node["source_sim_time_s"] = at
    return {"session_id": "one", "coordinate_frame": frame, "time_s": 1_700_000_000 + at,
            "source_sim_time_s": at, "published_monotonic_ns": int((100 + at) * 1e9),
            "nodes": [node] if z is not None else []}


def test_source_clock_gaps_and_invalid_frame_do_not_become_motion(tmp_path):
    raw, output = tmp_path / "raw", tmp_path / "out"
    path = raw / "logs/node_state.jsonl"
    write_rows(path, [snapshot(1, 20), snapshot(1, 20), snapshot(2),
                     snapshot(3, 200, stale=True), snapshot(4, 99, frame="NED"),
                     snapshot(5, 500, source_time=False), snapshot(6, 21)])
    path.write_text(path.read_text() + "{truncated\n")
    original = path.read_bytes()
    result = REPORT.export_live_metrics(raw, output, plots=False)
    with (output / "metrics/uav_trajectories.csv").open() as stream:
        rows = [row for row in csv.DictReader(stream) if row["uav"] == "uav1"]
    assert [float(row["source_sim_time_s"]) for row in rows] == [1, 2, 3, 4, 5, 6]
    assert [row["z_m"] for row in rows] == ["20.0", "", "", "", "", "21.0"]
    assert result["uavs"]["uav1"]["world_z_m"]["max"] == 21
    assert result["uavs"]["uav2"]["world_z_m"]["max"] is None
    assert result["target_status"].startswith("unavailable")
    assert result["input_errors"]["logs/node_state.jsonl:invalid_json"] == 1
    assert path.read_bytes() == original
    assert not (raw / "metrics").exists()


def test_only_timestamped_ENU_admission_is_a_target(tmp_path):
    raw = tmp_path / "raw"
    write_rows(raw / "logs/node_state.jsonl", [snapshot(1, 20)])
    base = {"uav_id": 1, "command_id": "1:1:8", "epoch": 1, "source_sim_time_s": 1,
            "host_monotonic_ns": 101_000_000_000, "target_position_m": [1, 2, 30],
            "target_frame": "ENU", "stage": "uart_admitted"}
    write_rows(raw / "logs/coordination_uav1.jsonl", [
        dict(base, stage="gcs_send"), dict(base, target_frame="NED"),
        dict(base, source_sim_time_s=None), base,
    ])
    result = REPORT.export_live_metrics(raw, tmp_path / "out", plots=False)
    assert result["coordination_events"] == 4
    assert result["uavs"]["uav1"]["admitted_target_samples"] == 1
    assert result["uavs"]["uav2"]["admitted_target_samples"] == 0
    assert result["uavs"]["uav1"]["world_z_m"]["max"] == 20


def test_report_exports_vector_plots_and_retains_partial_resource_sample(tmp_path):
    raw = tmp_path / "raw"
    write_rows(raw / "logs/node_state.jsonl", [snapshot(1, 20), snapshot(2), snapshot(3, 21)])
    write_rows(raw / "logs/runtime_resources.jsonl", [
        {"monotonic_ns": 2_000_000_000, "component": "sitl", "cpu_percent_one_core": 10, "rss_bytes": 1024},
        {"monotonic_ns": 2_000_000_000, "component": "sitl", "cpu_percent_one_core": None, "rss_bytes": 2048},
    ])
    output = tmp_path / "out"
    REPORT.export_live_metrics(raw, output)
    with (output / "metrics/runtime_resources.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["sum_cpu_percent_one_core"] == ""
    assert rows[0]["cpu_samples"] == "1"
    assert float(rows[0]["sum_process_rss_bytes"]) == 3072
    for stem in ["uav_height_trajectory", "live_timing_resources"]:
        for extension in ["png", "svg", "pdf"]:
            assert (output / "plots" / f"{stem}.{extension}").stat().st_size > 0


def test_end_to_end_rtf_retains_host_pauses_omitted_by_gazebo_stats(tmp_path):
    raw = tmp_path / "raw"
    first, paused, last = snapshot(1, 20), snapshot(1, 20), snapshot(3, 20)
    paused["published_monotonic_ns"] = 104_000_000_000
    last["published_monotonic_ns"] = 107_000_000_000
    write_rows(raw / "logs/node_state.jsonl", [first, paused, last])
    (raw / "logs/gazebo_stats.log").write_text(
        "sim_time { sec: 1 }\nreal_time { sec: 1 }\nreal_time_factor: 1\n\n"
        "sim_time { sec: 3 }\nreal_time { sec: 3 }\nreal_time_factor: 1\n")
    result = REPORT.export_live_metrics(raw, tmp_path / "out", plots=False)
    assert result["gazebo_integral_rtf_all_recorded_time"] == 1
    assert result["end_to_end_clock_segments"][0]["integral_rtf"] == 1 / 3
    assert result["end_to_end_window_rtf"]["min"] == 0
    assert result["end_to_end_window_wall_duration_s"]["min"] == 3


def test_explicit_phase_window_and_sent_intent_keep_their_own_meaning(tmp_path):
    raw = tmp_path / "raw"
    write_rows(raw / "logs/node_state.jsonl", [snapshot(1, 20), snapshot(2, 21), snapshot(3, 22)])
    write_rows(raw / "logs/scenario_events.jsonl", [
        {"event": "phase", "detail": "after_warmup", "monotonic_ns": 101_500_000_000},
        {"event": "flight_target", "uav": "uav1", "monotonic_ns": 102_100_000_000,
         "detail": json.dumps({"target_id": "takeoff", "target_enu_z_m": 50})},
    ])
    (raw / "metrics").mkdir()
    (raw / "metrics/scenario_summary.json").write_text(json.dumps({"status": "failed", "timestamp_domain": "realtime"}))
    result = REPORT.export_live_metrics(raw, tmp_path / "out", plots=False, start_phase="after_warmup")
    window = result["analysis_window"]
    assert window["requested_start_phase"] == "after_warmup"
    assert window["resolved_start_sim_time_s"] == 2
    assert window["phase_host_join_age_ms"] == 500
    assert result["uavs"]["uav1"]["position_samples"] == 2
    assert result["uavs"]["uav1"]["admitted_target_samples"] == 0
    assert result["sent_target_samples"] == 1
    assert result["end_to_end_window_rtf"]["p5"] == 1
    assert result["scenario_status"] == "failed"
    with (tmp_path / "out/metrics/uav_sent_targets.csv").open() as stream:
        target = next(csv.DictReader(stream))
    assert float(target["host_join_abs_age_ms"]) == 100
    assert target["target_z_m"] == "50.0"
    assert result["uavs"]["uav1"]["world_z_m"]["max"] == 22


def test_ambiguous_phase_clock_cannot_select_a_warmup_window(tmp_path):
    raw = tmp_path / "raw"
    write_rows(raw / "logs/node_state.jsonl", [snapshot(1, 20), snapshot(2, 20)])
    write_rows(raw / "logs/scenario_events.jsonl", [
        {"event": "phase", "detail": "ready", "monotonic_ns": 101_000_000_000},
    ])
    with pytest.raises(ValueError, match="no usable declared clock"):
        REPORT.export_live_metrics(raw, tmp_path / "out", plots=False, start_phase="ready")


def test_host_phase_during_a_model_pause_excludes_earlier_frozen_clock_samples(tmp_path):
    raw = tmp_path / "raw"
    first, paused, last = snapshot(1, 20), snapshot(1, 20), snapshot(2, 21)
    paused["published_monotonic_ns"] = 102_000_000_000
    last["published_monotonic_ns"] = 103_000_000_000
    write_rows(raw / "logs/node_state.jsonl", [first, paused, last])
    write_rows(raw / "logs/scenario_events.jsonl", [
        {"event": "phase", "detail": "ready", "clock_domain": "host_monotonic",
         "host_monotonic_ns": 101_900_000_000},
    ])
    result = REPORT.export_live_metrics(raw, tmp_path / "out", plots=False, start_phase="ready")
    assert result["analysis_window"]["resolved_start_host_monotonic_ns"] == 102_000_000_000
    assert result["end_to_end_clock_segments"][0]["integral_rtf"] == 1
