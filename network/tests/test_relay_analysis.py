"""Analysis fixtures are explicit synthetic logs, never integrated flight evidence."""

import csv
from copy import deepcopy
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
                           max_vertical_displacement_m=4.,
                           admitted_command_id=body["command_id"]))
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    with (run / "metrics/relay_control_refresh.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["request_id", "uav_id", "host_monotonic_ns", "command_id"])
        writer.writeheader(); writer.writerows(offers)
    with (run / "logs/native_radio_events.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["event", "wall_monotonic_ns", "time_s", "peer"])
        writer.writeheader()
        if condition != "none":
            writer.writerows([dict(event="fault_start", wall_monotonic_ns=55 * NS, time_s=55, peer="uav1"),
                              dict(event="fault_end", wall_monotonic_ns=115 * NS, time_s=115, peer="uav1")])
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


def test_total_isolation_counts_lost_renewals_separately_from_new_appointments(tmp_path):
    run, events = fixture(tmp_path, condition="total_GCS")
    events = [row for row in events if row.get("event") == "grant_issued" and row["epoch"] == 1]
    events.append(dict(events[0], host_monotonic_ns=55.2 * NS, grant_sequence=3,
                       valid_from_ns=55.2 * NS, valid_until_ns=57.2 * NS, reason="renewal"))
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["total_GCS"]["all_grants_issued_during_fault"] == 1
    assert result["total_GCS"]["new_leader_selection_grants_during_fault"] == 0
    assert result["total_GCS"]["no_new_leader_selection"] is True
    assert result["total_GCS"]["hold_all_five"] is False


def test_frozen_plan_has_thirty_paired_rows_and_reproducible_random_order():
    plan = REPORT.campaign_plan()
    assert len(plan) == 30
    assert plan == REPORT.campaign_plan()
    assert {row["rng_seed"] for row in plan} == {1}
    for condition in REPORT.CONDITIONS:
        for run in range(1, 6):
            rows = [row for row in plan if row["fault_profile"] == condition and row["rng_run"] == run]
            assert {row["policy"] for row in rows} == {"fixed", "failover"}
            assert abs(rows[0]["order"] - rows[1]["order"]) == 1
    assert {plan[index]["policy"] for index in range(0, 30, 2)} == {"fixed", "failover"}


def test_bootstrap_unit_is_a_pair_and_one_pair_does_not_get_an_interval():
    result = REPORT.paired_interval([-4, -3, -2, -1, 0])
    assert result["n_pairs"] == 5
    assert result["mean_difference"] == -2
    assert result["bootstrap_resamples"] == 3125
    assert result["interval_95"][0] < -2 < result["interval_95"][1]
    assert REPORT.paired_interval([-2])["interval_95"] is None


def test_failed_run_remains_an_unconditional_outcome_when_network_window_is_incomplete(tmp_path):
    run, _ = fixture(tmp_path)
    base = analyze(run, tmp_path)
    base["pilot"] = False
    metadata = base["metadata"]
    metadata["environment"].update(git_head="f" * 40, source_dirty_paths="0",
        wifi_explicit_bar_after_missed_block_ack_requested="1", wifi_aggregation_requested="1")
    metadata["native_stats"].update(rng_seed=1, rng_run=1,
        wifi_explicit_bar_after_missed_block_ack=True, wifi_aggregation_enabled=True)
    metadata["configuration_snapshot"] = {"container_image_id": "sha256:unit-fixture"}
    metadata["config"].update({key: 1 for key in REPORT.MISSION_FIELDS})
    metadata["config"]["effective_service_args"] = {key: 1 for key in REPORT.SERVICE_FIELDS}
    pair = [row for row in REPORT.campaign_plan(repeats=1) if row["fault_profile"] == "selective"]
    records = []
    for planned in pair:
        result = deepcopy(base)
        result["policy"] = planned["policy"]
        failed = planned["policy"] == "fixed"
        result["run_status"] = "failed" if failed else "completed"
        result["observation_window"]["complete"] = not failed
        result["criteria"] = {"five_uav_lifecycle": not failed}
        records.append(REPORT.campaign_record(planned, result))
    aggregate = REPORT.aggregate_campaign(records)
    assert aggregate["run_status_counts"] == {"failed": 1, "completed": 1}
    status_effect = next(row for row in aggregate["paired_points"] if row["metric"] == "run_completed")
    assert status_effect["difference"] == 1
    assert not any(row["metric"] == "timely_per_offered" for row in aggregate["paired_points"])
    assert aggregate["pair_exclusions"]


def test_missing_campaign_runs_do_not_turn_into_zero_measurements(tmp_path):
    rows = REPORT.campaign_plan(repeats=1)
    plan = tmp_path / "plan.csv"
    REPORT.write_csv(plan, rows, list(rows[0]))
    out = tmp_path / "campaign"
    result = REPORT.analyze_campaign(plan, tmp_path / "not-recorded", out, plots=True, live_export=False)
    assert result["recorded_runs"] == 0
    assert result["run_status_counts"] == {"not_run": 6}
    assert result["paired_estimates"] == []
    assert all(row["outcomes"] == {} for row in result["records"])
    for extension in ("png", "svg", "pdf"):
        assert (out / f"plots/relay_campaign_pairs.{extension}").stat().st_size > 0


def test_single_run_vector_plots_preserve_a_censored_vehicle(tmp_path):
    run, _ = fixture(tmp_path, missing_uav=2)
    out = tmp_path / "out"
    result = REPORT.analyze_run(run, out, plots=True, live_export=False)
    assert result["control_recovery_per_uav"]["2"]["status"] == "right_censored"
    for extension in ("png", "svg", "pdf"):
        assert (out / f"plots/relay_outcomes.{extension}").stat().st_size > 0


def test_changed_implementations_are_not_pooled_into_one_campaign_interval():
    base = dict(recorded=True, run_status="completed", policy="fixed", fault_profile="selective",
                network_eligible=True, study_eligible=True, exclusion_reasons=[],
                outcomes={"run_completed": 1.}, comparability={"head": "one"})
    records = [dict(base, pair_id="one"), dict(base, pair_id="one", policy="failover"),
               dict(base, pair_id="two", comparability={"head": "two"}),
               dict(base, pair_id="two", policy="failover", comparability={"head": "two"})]
    result = REPORT.aggregate_campaign(records)
    assert result["recorded_runs"] == 4
    assert result["global_frozen_configuration_consistent"] is False
    assert result["paired_estimates"] == []


def test_foreign_run_cannot_supply_recovery_and_true_flag_requires_measured_displacement(tmp_path):
    run, events = fixture(tmp_path)
    for row in events:
        if row.get("event") == "uart_admitted" and row.get("uav_id") == 1 and row.get("epoch") == 2:
            row["run_id"] = "foreign-run"
        if row.get("goal_id") == "after-3":
            row["max_vertical_displacement_m"] = .5
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["control_recovery_per_uav"]["1"]["recovered"] is False
    assert result["goal_recovery_per_uav"]["3"]["recovered"] is False
    assert any("foreign-run" in value for value in result["data_quality_errors"])


def test_coordinator_selected_before_fault_is_not_a_postfault_handover(tmp_path):
    run, events = fixture(tmp_path)
    for row in events:
        if row.get("event") == "grant_issued" and row["epoch"] == 1:
            row["valid_until_ns"] = 54 * NS
        elif row.get("event") == "grant_issued" and row["epoch"] == 2:
            row["host_monotonic_ns"] = 54 * NS
            row["valid_from_ns"] = 54 * NS
        elif row.get("epoch") == 2:
            row["valid_from_ns"] = 54 * NS
        elif row.get("epoch") == 1:
            row["valid_until_ns"] = 54 * NS
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = analyze(run, tmp_path)
    assert result["pre_fault_authority"]["leader_id"] == 2
    assert result["handover_recovery"]["all_five"]["recovered"] is False
    assert any("actual pre-fault coordinator" in value for value in result["data_quality_errors"])


def test_unfinished_artifacts_are_neither_a_failed_result_nor_a_completed_run(tmp_path):
    run, _ = fixture(tmp_path)
    result = analyze(run, tmp_path)
    result["run_status"] = "not_recorded"
    planned = next(row for row in REPORT.campaign_plan(repeats=1) if row["policy"] == "failover" and row["fault_profile"] == "selective")
    record = REPORT.campaign_record(planned, result)
    assert record["has_artifacts"] is True
    assert record["recorded"] is False
    assert record["outcomes"]["run_completed"] is None


def test_fixed_policy_cannot_credit_a_different_master_as_its_recovery(tmp_path):
    run, _ = fixture(tmp_path)
    for relative in ["metrics/relay_experiment_summary.json", "metrics/relay_experiment_config.json"]:
        value = json.loads((run / relative).read_text())
        value["policy"] = "fixed"
        (run / relative).write_text(json.dumps(value))
    result = analyze(run, tmp_path)
    assert result["control_recovery"]["all_five"]["recovered"] is False
    assert result["handover_recovery"]["all_five"]["recovered"] is None
    assert result["policy_audit"]["fixed_policy_new_leader_appointments"] == 1
    assert result["rates"]["all_five"]["timely"] == 0
    assert result["goal_recovery_all_five"]["recovered"] is False


def test_signature_excludes_run_paths_but_includes_mac_modes_and_effective_service_thresholds(tmp_path):
    run, _ = fixture(tmp_path)
    base = analyze(run, tmp_path)
    base["pilot"] = False
    meta = base["metadata"]
    meta["environment"].update(git_head="f" * 40, source_dirty_paths="0",
        wifi_explicit_bar_after_missed_block_ack_requested="0", wifi_aggregation_requested="0")
    meta["native_stats"].update(rng_seed=1, rng_run=1, wifi_data_mode="HtMcs0", wifi_control_mode="ErpOfdmRate6Mbps",
        wifi_explicit_bar_after_missed_block_ack=False, wifi_aggregation_enabled=False)
    meta["configuration_snapshot"] = {"container_image_id": "sha256:unit-fixture"}
    meta["config"].update({key: 1 for key in REPORT.MISSION_FIELDS})
    meta["config"]["effective_service_args"] = {key: 1 for key in REPORT.SERVICE_FIELDS}
    planned = next(row for row in REPORT.campaign_plan(repeats=1) if row["policy"] == "failover" and row["fault_profile"] == "selective")
    first = REPORT.campaign_record(planned, base)
    changed_paths = deepcopy(base)
    changed_paths["metadata"]["config"].update(run_id="another-run", run_dir="/another/run", node_state="/tmp/another/state", phase_file="/another/phase")
    changed_paths["metadata"]["config"]["effective_service_args"].update(event_log="/another/log", radio_watchdog_file="/another/watchdog", run_id="another-run")
    assert REPORT.campaign_record(planned, changed_paths)["comparability"] == first["comparability"]
    for change in ("mac", "command_age", "landing", "mode"):
        value = deepcopy(base)
        if change == "mac":
            value["metadata"]["native_stats"]["wifi_aggregation_enabled"] = True
            value["metadata"]["environment"]["wifi_aggregation_requested"] = "1"
        elif change == "command_age":
            value["metadata"]["config"]["effective_service_args"]["command_max_age_ms"] = 100
        elif change == "landing":
            value["metadata"]["config"]["land_z_tolerance_m"] = .5
        else:
            value["metadata"]["native_stats"]["wifi_data_mode"] = "HtMcs1"
        assert REPORT.campaign_record(planned, value)["comparability"] != first["comparability"]
    missing_effective = deepcopy(base)
    missing_effective["metadata"]["native_stats"].pop("wifi_aggregation_enabled")
    record = REPORT.campaign_record(planned, missing_effective)
    assert record["comparability"]["radio"]["wifi_aggregation_enabled"] is None
    assert record["study_eligible"] is False
