"""Execute the diagnostic report branch; no simulator or substituted report helpers."""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "native_latency_report", ROOT / "scripts/product/summarize_native_radio_five_uav.py"
)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def fixture_run(tmp_path, status="limited", kind="serial", events=(), configured=False):
    run = tmp_path / f"diagnostic-{kind}"
    (run / "metrics").mkdir(parents=True)
    (run / "logs").mkdir()
    (run / "external_endpoint").mkdir()
    (run / "external_endpoint/metrics.json").write_text(json.dumps({
        "controller_kind": kind, "hardware_validation": "not_performed",
        "connected": False, "radio_live": False,
    }))
    if status is not None:
        (run / "metrics/scenario_summary.json").write_text(json.dumps({
            "status": status, "latency_diagnostic": {"uav_count": 5, "channels": ["control", "payload"]},
        }))
    if configured:
        (run / "logs/native_sources.json").write_text(json.dumps({
            "segments": [{"id": "source1", "start_s": 10, "stop_s": 20, "power_w": .001}]
        }))
    fields = ["time_s", "wall_monotonic_ns", "phase", "event", "node", "peer",
              "bytes", "x", "y", "z", "value", "details"]
    with (run / "logs/native_radio_events.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for time_s, event, value, details in events:
            writer.writerow(dict(time_s=time_s, wall_monotonic_ns=int(time_s * 1e9),
                phase="diagnostic", event=event, node="cp", peer="source1",
                bytes=0, x=0, y=0, z=0, value=value, details=details))
    return run


@pytest.mark.parametrize("kind", ["serial", "udp", "tcp_client"])
@pytest.mark.parametrize("status,exit_code", [("failed", 1), ("limited", 1), ("diagnostic_complete", 0)])
def test_endpoint_reports_execute_and_keep_original_status(tmp_path, monkeypatch, kind, status, exit_code):
    run = fixture_run(tmp_path, kind=kind, status=status)
    before = (run / "metrics/scenario_summary.json").read_bytes()
    endpoint_before = (run / "external_endpoint/metrics.json").read_bytes()
    monkeypatch.setattr(sys, "argv", ["report", "--run-dir", str(run), "--latency-diagnostic"])
    assert REPORT.main() == exit_code
    summary = json.loads((run / "metrics/control_latency_summary.json").read_text())
    assert summary["status"] == status
    assert summary["mavlink_latency"]["first_attempt_rtt_ms"]["p95"] is None
    assert f"Status: **{status}**" in (run / "report.md").read_text()
    assert (run / "plots/native_overview.png").stat().st_size > 0
    assert (run / "metrics/scenario_summary.json").read_bytes() == before
    assert (run / "external_endpoint/metrics.json").read_bytes() == endpoint_before
    assert "functional_five_uav_native_path" not in summary


def test_actual_source_samples_reach_diagnostic_report(tmp_path):
    run = fixture_run(tmp_path, configured=True, events=[
        (10, "jammer_on", .001, "source=ns3::WaveformGenerator"),
        (12, "spectrum_signal_arrival", -67, "foreign_signal=1"),
        (12, "phy_rx_error", 2, ""),
        (20, "jammer_off", .001, "source=ns3::WaveformGenerator"),
    ])
    assert REPORT.write_latency_diagnostic_report(run) == 1
    source = json.loads((run / "metrics/native_source_summary.json").read_text())
    assert source["configured_sources"]["segments"][0]["id"] == "source1"
    active = source["windows"]["active"]
    assert active["foreign_power_dbm_by_receiver"]["cp"]["mean"] == -67
    assert active["decoder_failure_attempts"] == 1
    assert active["sinr_db"]["mean"] is None
    assert active["application_pdr"] is None
    assert "## Native Spectrum sources" in (run / "report.md").read_text()


@pytest.mark.parametrize("events,configured", [
    ([], False), ([], True),
    ([(10, "jammer_on", .001, "")], True),
    ([(20, "jammer_off", .001, "")], True),
    ([(20, "jammer_on", .001, ""), (10, "jammer_off", .001, "")], True),
])
def test_missing_or_incomplete_source_measurements_do_not_become_zero_windows(tmp_path, events, configured):
    run = fixture_run(tmp_path, events=events, configured=configured, status=None)
    assert REPORT.write_latency_diagnostic_report(run) == 1
    source = json.loads((run / "metrics/native_source_summary.json").read_text())
    assert source["windows"] == {}
    assert source["window_availability"] != "observed_switch_boundaries"
    assert len(source["sources"]) == len(events)
    summary = json.loads((run / "metrics/control_latency_summary.json").read_text())
    assert summary["status"] is None
    assert summary["mavlink_latency"]["first_attempt_rtt_ms"]["p95"] is None
    assert "Status: **missing**" in (run / "report.md").read_text()
    if not events:
        assert "## Native Spectrum sources" not in (run / "report.md").read_text()


@pytest.mark.parametrize("native_log", [None, "time_s,event\nmalformed,jammer_on\n"])
def test_unavailable_native_trace_still_reports_missing_data(tmp_path, native_log):
    run = fixture_run(tmp_path, status="failed", configured=True)
    path = run / "logs/native_radio_events.csv"
    if native_log is None:
        path.unlink()
    else:
        path.write_text(native_log)
    assert REPORT.write_latency_diagnostic_report(run) == 1
    source = json.loads((run / "metrics/native_source_summary.json").read_text())
    assert source["windows"] == {}
    summary = json.loads((run / "metrics/control_latency_summary.json").read_text())
    assert summary["status"] == "failed"
    assert summary["mavlink_latency"]["first_attempt_rtt_ms"]["samples"] == 0
    assert summary["mavlink_latency"]["first_attempt_rtt_ms"]["p95"] is None
