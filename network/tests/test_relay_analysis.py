"""Analysis fixtures are explicit synthetic logs, never integrated flight evidence."""

import csv
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("relay_analysis", ROOT / "scripts/product/analyze_relay_experiment.py")
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
NS = 1_000_000_000


def fixture(tmp_path, condition="selective", missing_uav=None):
    run = tmp_path / "raw"
    (run / "logs").mkdir(parents=True)
    (run / "metrics").mkdir()
    config = {"initial_master": 1, "policy": "failover", "fault_profile": condition,
              "hold_s": 2, "duration_s": 180, "pilot": True}
    summary = dict(config, run_id="unit-fixture", status="completed",
        measurement_start_host_monotonic_ns=10 * NS, measurement_start_source_sim_time_s=10,
        measurement_duration_wall_s=180, measurement_duration_source_s=180)
    (run / "metrics/relay_experiment_config.json").write_text(json.dumps(config))
    (run / "metrics/relay_experiment_summary.json").write_text(json.dumps(summary))
    events = [dict(event="grant_issued", host_monotonic_ns=NS, leader_id=1, epoch=1,
                   grant_sequence=1, valid_from_ns=NS, valid_until_ns=56 * NS, reason="selection"),
              dict(event="grant_issued", host_monotonic_ns=56 * NS, leader_id=2, epoch=2,
                   grant_sequence=2, valid_from_ns=56 * NS, valid_until_ns=65 * NS, reason="selection")]
    offers = []
    for i in range(1, 6):
        created = (58 + i / 100) * NS
        body = dict(run_id="unit-fixture", uav_id=i, leader_id=2, epoch=2,
            command_id=f"2:{i}:{i}", grant_sequence=2,
            valid_from_ns=56 * NS, valid_until_ns=65 * NS,
            command_created_host_monotonic_ns=created, payload_sha256=f"fixture-bytes-{i}",
            target_position_m=[i, 0, 14], target_frame="ENU")
        for offset, (name, actor) in enumerate([("gcs_send", 0), ("master_receive", 2),
                *(([("master_forward", 2)]) if i != 2 else []), ("follower_receive", i), ("uart_admitted", i)]):
            if name == "uart_admitted" and i == missing_uav:
                continue
            events.append(dict(body, event=name, stage=name, node_id=actor,
                host_monotonic_ns=created + offset * 10_000_000,
                admission_checked_host_monotonic_ns=created + offset * 10_000_000 - 10,
                uart_write_started_host_monotonic_ns=created + offset * 10_000_000 - 30,
                uart_write_end_host_monotonic_ns=created + offset * 10_000_000 - 20))
        # Additional observed input establishes the pre-fault active target,
        # independently of the last completed goal and refresh offer fixtures.
        events.append(dict(body, event="uart_admitted", epoch=1, leader_id=1, grant_sequence=1,
            command_id=f"1:{i}:initial", node_id=i, target_position_m=[i, 0, 10],
            command_created_host_monotonic_ns=53 * NS, valid_from_ns=NS, valid_until_ns=56 * NS,
            host_monotonic_ns=54 * NS, admission_checked_host_monotonic_ns=54 * NS - 10,
            uart_write_started_host_monotonic_ns=54 * NS - 30, uart_write_end_host_monotonic_ns=54 * NS - 20))
        offers.append(dict(request_id=f"sent-{i}", uav_id=i, host_monotonic_ns=created, command_id=body["command_id"]))
        offers.append(dict(request_id=f"unavailable-{i}", uav_id=i, host_monotonic_ns=59 * NS, command_id=""))
        events.append(dict(event="action_completed", uav_id=i, host_monotonic_ns=50 * NS,
                           target_position_m=[i, 0, 10], target_frame="ENU", goal_id=f"before-{i}"))
        events.append(dict(body, event="action_completed", host_monotonic_ns=61 * NS, goal_id=f"after-{i}",
                           goal_created_host_monotonic_ns=57 * NS, maneuver_observed=True, dwell_s=2,
                           admitted_command_id=body["command_id"]))
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    with (run / "metrics/relay_control_refresh.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["request_id", "uav_id", "host_monotonic_ns", "command_id"])
        writer.writeheader(); writer.writerows(offers)
    with (run / "logs/native_radio_events.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["event", "wall_monotonic_ns", "time_s"])
        writer.writeheader()
        if condition != "none":
            writer.writerows([dict(event="fault_start", wall_monotonic_ns=55 * NS, time_s=55),
                              dict(event="fault_end", wall_monotonic_ns=115 * NS, time_s=115)])
    return run, events


def analyze(run, tmp_path):
    return REPORT.analyze_run(run, tmp_path / "out", plots=False, live_export=False)


def test_losses_stay_in_sent_denominator_and_all_five_is_not_four(tmp_path):
    run, _ = fixture(tmp_path, missing_uav=2)
    result = analyze(run, tmp_path)
    assert result["rates"]["all_five"]["offered"] == 10
    assert result["rates"]["all_five"]["sent_per_offered"] == .5
    assert result["rates"]["all_five"]["timely_per_sent"] == .8
    assert result["rates"]["all_five"]["timely_per_offered"] == .4
    assert result["control_recovery"]["all_five"]["status"] == "right_censored"
    assert result["control_recovery"]["all_five"]["time_s"] is None
    assert result["control_recovery"]["all_five"]["restricted_unrecovered_time_s"] == 30
    assert result["control_recovery"]["four_followers"]["members"] == [1, 3, 4, 5]
    assert result["control_recovery"]["four_followers"]["recovered"] is True


def test_no_fault_has_no_recovery_and_complete_goal_is_not_a_refresh_ack(tmp_path):
    run, _ = fixture(tmp_path, condition="none")
    result = analyze(run, tmp_path)
    assert result["control_recovery"]["all_five"]["time_s"] is None
    assert result["control_recovery"]["all_five"]["recovered"] is None
    assert result["goal_recovery_per_uav"]["1"]["time_s"] is None


def test_goal_must_be_created_after_fault_and_physically_maneuvered(tmp_path):
    run, events = fixture(tmp_path)
    for row in events:
        if row.get("goal_id") == "after-1":
            row["goal_created_host_monotonic_ns"] = 54 * NS
        if row.get("goal_id") == "after-2":
            row["maneuver_observed"] = False
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["goal_recovery_per_uav"]["1"]["recovered"] is False
    assert result["goal_recovery_per_uav"]["2"]["recovered"] is False
    assert result["goal_recovery_per_uav"]["3"]["time_s"] == 6
    assert result["completion_gap_s_per_uav"]["3"] == 11


def test_log_emit_after_expiry_is_not_stale_if_exact_write_check_was_valid(tmp_path):
    run, events = fixture(tmp_path)
    admitted = next(row for row in events if row.get("event") == "uart_admitted")
    admitted["host_monotonic_ns"] = 65 * NS + 10
    admitted["admission_checked_host_monotonic_ns"] = 65 * NS - 10
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["admission_audit"]["valid"] == 10
    assert result["admission_audit"].get("expired_or_not_yet_valid", 0) == 0


def test_missing_exact_boundary_is_unverifiable_not_a_claim_of_zero_stale(tmp_path):
    run, events = fixture(tmp_path)
    for row in events:
        if row.get("event") == "uart_admitted":
            row.pop("admission_checked_host_monotonic_ns")
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["unverifiable_admissions"] == 10
    assert result["control_recovery"]["all_five"]["recovered"] is False


def test_active_prefault_target_not_last_completed_target_defines_a_new_maneuver(tmp_path):
    run, events = fixture(tmp_path)
    for row in events:
        if row.get("uav_id") != 3:
            continue
        if row.get("command_id") == "1:3:initial":
            row["target_position_m"] = [3, 0, 14]
        elif row.get("command_id") == "2:3:3":
            row["target_position_m"] = [3, 0, 10]
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["goal_recovery_per_uav"]["3"]["recovered"] is True
    assert result["completion_gap_s_per_uav"]["3"] == 11


def test_interval_and_actual_write_end_are_independently_audited(tmp_path):
    run, events = fixture(tmp_path)
    admitted = next(row for row in events if row.get("event") == "uart_admitted" and row["epoch"] == 2)
    admitted["uart_write_end_host_monotonic_ns"] = 65 * NS + 1
    admitted["admission_checked_host_monotonic_ns"] = 65 * NS - 1
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["admission_audit"]["write_outside_lease"] == 1
    assert result["control_recovery_per_uav"]["1"]["recovered"] is False


def test_missing_relay_stage_cannot_be_a_timely_success(tmp_path):
    run, events = fixture(tmp_path)
    events = [row for row in events if not (row.get("uav_id") == 1 and row.get("event") == "master_forward")]
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["rates"]["uav1"]["timely"] == 0
