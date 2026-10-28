"""Explicit synthetic analysis fixtures, never evidence of integrated flight."""
import csv
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("urban_analysis", ROOT/"scripts/product/analyze_urban_experiment.py")
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)
SPEC_OLD = importlib.util.spec_from_file_location("relay_fixture", ROOT/"network/tests/test_relay_analysis.py")
OLD = importlib.util.module_from_spec(SPEC_OLD)
SPEC_OLD.loader.exec_module(OLD)
NS = 1_000_000_000


def geometry(tmp_path, rows):
    path = tmp_path/"geometry.csv"
    with path.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["time_s", "wall_monotonic_ns", "from_id", "to_id", "geom_los", "radio_state", "pathloss_db"])
        writer.writeheader()
        writer.writerows(rows)
    errors = []
    output = A.load_geometry(path, errors)
    assert errors == []
    return output


def test_trigger_is_transition_with_prior_state_not_first_nlos_sample(tmp_path):
    rows = geometry(tmp_path, [dict(time_s=t, wall_monotonic_ns=t*NS, from_id=0, to_id=1, geom_los=los)
                               for t,los in [(9, False), (11, False), (12, True), (13, False)]])
    result = A.geometry_transitions(rows, 10*NS, 20*NS)
    assert [(r["transition"], A.stamp(r)/NS) for r in result] == [("LOS_return",12), ("NLOS_entry",13)]
    assert result[-1]["transition_bracket_s"] == 1


def test_other_link_shadow_does_not_define_primary_trigger(tmp_path):
    rows = geometry(tmp_path, [dict(time_s=t, wall_monotonic_ns=t*NS, from_id=0, to_id=peer, geom_los=los)
        for t,peer,los in [(9,2,True), (11,2,False), (12,1,True), (13,1,False)]])
    transitions = A.geometry_transitions(rows, 10*NS, 20*NS)
    chosen = next(r for r in transitions if r["pair"] == "0-1" and r["transition"] == "NLOS_entry")
    assert A.stamp(chosen) == 13*NS


def test_administrative_nonrecovery_differs_from_crash_missing_data_and_absent_event():
    assert A.endpoint(None, 10*NS, 20*NS, full_window=True)["status"] == "administratively_right_censored"
    interrupted = A.endpoint(None, 10*NS, 15*NS, full_window=False)
    assert interrupted["status"] == "interrupted_attempt" and interrupted["censor_time_s"] is None
    assert A.endpoint(None, None, 20*NS, full_window=True)["recovered"] is None
    assert A.endpoint(None, 10*NS, 20*NS, full_window=True, data_valid=False)["status"] == "unverifiable_recording"
    assert A.endpoint(12*NS, 10*NS, 15*NS, full_window=False)["time_s"] == 2


def test_all_five_uart_requires_one_epoch_and_rejects_queued_prefault_command():
    def command(i, epoch=2, created=11):
        return dict(uav_id=i, leader_id=2, epoch=epoch, command_id=f"{epoch}:{i}:1", created_ns=created*NS,
            admitted_ns=(12+i/10)*NS, lease_status="valid", lineage_status="valid",
            application_chain_complete=True, policy_allowed=True)
    rows = [command(i, epoch=3 if i == 5 else 2) for i in range(1,6)]
    result = A.uart_endpoints(rows, 10*NS, 20*NS, True, True)
    assert all(result[f"uav{i}"]["recovered"] for i in range(1,6))
    assert result["all_five"]["recovered"] is False
    rows[-1] = command(5, created=9)
    result = A.uart_endpoints(rows, 10*NS, 20*NS, True, True)
    assert result["uav5"]["recovered"] is False
    assert A.uart_endpoints(rows, 10*NS, 20*NS, True, True, handover=True)["all_five"]["status"] == "not_applicable_no_handover"


def test_same_epoch_renewals_are_not_conflicts_but_old_future_authority_is():
    rows = [dict(event="grant_issued", grant_sequence=seq, leader_id=leader, epoch=epoch,
                 valid_from_ns=low, valid_until_ns=high)
            for seq,leader,epoch,low,high in [(1,1,1,0,20), (2,1,1,10,30), (3,2,2,25,40)]]
    result = A.lease_conflicts(rows)
    assert result == [dict(old_sequence=2, new_sequence=3, overlap_ns=5)]


def test_physical_dwell_is_not_proved_by_completion_event_or_filled_gap():
    completion = dict(uav_id=1, goal_id="waypoint0:uav1", source_sim_time_s=13,
        dwell_s=2, target_position_m=[10,10,12], target_frame="ENU", host_monotonic_ns=13*NS)
    poses = [dict(uav_id=1, session_id="unit", source_sim_time_s=t, quality="observed", x_m=10,y_m=10,z_m=12,speed_mps=0)
             for t in (11,11.5,12,12.5,13)]
    assert A.replay_dwell(completion, poses, {})["status"] == "verified"
    assert A.replay_dwell(completion, poses[:2]+poses[3:], {})["status"] == "not_verified"
    poses[2]["quality"] = "stale_or_fault"
    assert A.replay_dwell(completion, poses, {})["status"] == "not_verified"


def fixture(tmp_path):
    run, events = OLD.fixture(tmp_path, condition="none")
    for row in events:
        if row.get("goal_id", "").startswith("after-"):
            row["goal_id"] = f"waypoint0:uav{row['uav_id']}"
            row["waypoint_index"] = 0
            row["source_sim_time_s"] = 61
    for row in events:
        if row.get("event") == "gcs_send":
            row["goal_id"] = f"waypoint0:uav{row['uav_id']}"
            row["goal_created_host_monotonic_ns"] = 57*NS
    (run/"logs/coordination_events.jsonl").write_text("".join(json.dumps(row)+"\n" for row in events))
    with (run/"logs/geometry_links.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["time_s", "wall_monotonic_ns", "from_id", "to_id", "geom_los", "radio_state", "pathloss_db"])
        writer.writeheader()
        writer.writerows(dict(time_s=t, wall_monotonic_ns=t*NS, from_id=0, to_id=1, geom_los=los,
                              radio_state="LOS" if los else "NLOS", pathloss_db=70 if los else 95)
                         for t,los in [(54,True), (55,False), (62,True)])
    snapshots = [dict(published_monotonic_ns=t*NS, session_id="fixture", source_sim_time_s=t, coordinate_frame="ENU",
                     nodes=[dict(id=f"uav{i}", source_sim_time_s=t, sample_monotonic_ns=t*NS, position_m=[i,0,14],
                                 velocity_enu_mps=[0,0,0], stale=False) for i in range(1,6)])
                 for t in (58.5,59,59.5,60,60.5,61)]
    (run/"logs/node_state.jsonl").write_text("".join(json.dumps(row)+"\n" for row in snapshots))
    scenario = tmp_path/"scenario.json"
    scenario.write_text(json.dumps(dict(routes={str(i):[[i,0,14]] for i in range(1,6)})))
    return run, scenario


def test_full_export_keeps_offered_missing_uart_and_waypoints_separate(tmp_path):
    run, scenario = fixture(tmp_path)
    result = A.analyze(run, tmp_path/"export", scenario_path=scenario)
    assert result["rates"]["all_five"]["offered"] == 10
    assert result["rates"]["all_five"]["timely_per_offered"] == .5
    assert result["trigger"]["event"]["host_monotonic_ns"] == 55*NS
    assert result["uart_after_NLOS"]["all_five"]["recovered"] is True
    assert result["physical_checks"] == dict(total=5, verified=5)
    assert all(row["completed_fraction"] == 1 for row in result["mission_progress"].values())
    assert "not command ACK" in result["provenance"]["physical_evidence"]
    assert result["post_LOS_return"]["offered"] == 0
    assert result["post_LOS_return"]["admitted_per_offered"] is None
    assert result["data_quality_errors"] == []


def test_raw_directory_output_is_rejected(tmp_path):
    run, scenario = fixture(tmp_path)
    try:
        A.analyze(run, run/"analysis", scenario_path=scenario)
    except ValueError as error:
        assert "immutable raw" in str(error)
    else:
        raise AssertionError("raw tree was writable by exporter")


def test_plan_has_four_factorial_cases_per_seed_and_no_fake_freeze():
    plan = A.study_plan(5)
    assert plan == A.study_plan(5)
    assert len(plan) == 20
    blocks = {}
    for row in plan:
        blocks.setdefault(row["block_id"], []).append(row)
        assert row["status"] == "planned_not_frozen_not_run"
    assert all({(r["policy"],r["radio_mode"]) for r in rows} ==
               {(p,m) for p in ("fixed","failover") for m in ("geometry","los_only")} for rows in blocks.values())


def test_runtime_contract_accepts_explicit_geometry_and_rejects_double_pathloss():
    spec = importlib.util.spec_from_file_location("urban_live_summary", ROOT/"scripts/product/summarize_native_radio_five_uav.py")
    summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary)
    config = summary.load_scenario_config(ROOT/"network/config/scenario_5uav_rock_demo_native_product.yaml")
    radio, rt = config["radio"], config["sionna"]
    stats = dict(uav_count=5, radio_node_count=6, shared_spectrum_channel_count=1,
        native_ns3_phy=True, native_ns3_mac=True, custom_packet_error_model=False, custom_scheduler=False,
        radio_backend="wifi", profile=radio["profile"], technology_specific_modem=False,
        neighbor_discovery_mode=radio["neighbor_discovery_mode"], wifi_data_mode=radio["data_mode"],
        wifi_control_mode=radio["control_mode"], wifi_ssid=radio["ssid"],
        wifi_channel_number=radio["channel_number"], wifi_actual_channel_number=radio["channel_number"],
        tx_power_w=radio["tx_power_w"], carrier_hz=radio["carrier_hz"],
        wifi_actual_center_frequency_hz=radio["carrier_hz"], wifi_channel_width_mhz=radio["channel_width_mhz"],
        readiness_lag_max_ms=rt["readiness_lag_max_ms"], selected_propagation_profile="geometry_logdistance",
        scalar_base_propagation_model_count=1, phased_array_spectrum_propagation_model_count=0,
        rx_psd_propagation_model="bas::GeometryLogDistance", sionna_in_process=False, sionna_drives_rx_psd=False,
        packet_outcome_affected=True, pose_snapshots=10, stale_pose_samples=0, geometry_radio_mode="geometry",
        geometry_parameter_set="unit_assumption", geometry_packet_evaluations=15, geometry_observation_rows=30)
    assert summary.native_product_runtime_contract(stats, config)["passed"]
    stats["phased_array_spectrum_propagation_model_count"] = 1
    result = summary.native_product_runtime_contract(stats, config)
    assert not result["passed"] and not result["checks"]["no_second_spectrum_pathloss"]
    stats["phased_array_spectrum_propagation_model_count"] = 0
    stats["sionna_in_process"] = True
    assert not summary.native_product_runtime_contract(stats, config)["passed"]


def test_pose_freshness_is_verified_from_clocks_not_only_stale_flag(tmp_path):
    row = dict(published_monotonic_ns=10*NS, session_id="fixture", source_sim_time_s=10, coordinate_frame="ENU",
        nodes=[dict(id="uav1", source_sim_time_s=10, sample_monotonic_ns=9*NS,
                    position_m=[0,0,10], velocity_enu_mps=[0,0,0], stale=False)])
    path = tmp_path/"poses.jsonl"
    path.write_text(json.dumps(row)+"\n")
    errors = []
    poses = A.read_poses(path, 9*NS, 11*NS, errors)
    assert poses[0]["quality"] == "stale_or_future_sample_clock"
    assert poses[0]["x_m"] is None
    row["nodes"][0]["sample_monotonic_ns"] = 10*NS+1
    path.write_text(json.dumps(row)+"\n")
    assert A.read_poses(path, 9*NS, 11*NS, [])[0]["quality"] == "stale_or_future_sample_clock"


def test_physical_goal_cannot_complete_after_lease_or_at_wrong_configured_point(tmp_path):
    run, scenario = fixture(tmp_path)
    events = [json.loads(line) for line in (run/"logs/coordination_events.jsonl").read_text().splitlines()]
    for row in events:
        if row.get("event") == "action_completed" and row.get("waypoint_index") == 0:
            row["host_monotonic_ns"] = 66*NS
    (run/"logs/coordination_events.jsonl").write_text("".join(json.dumps(r)+"\n" for r in events))
    result = A.analyze(run, tmp_path/"expired", scenario_path=scenario)
    assert result["physical_checks"]["verified"] == 0
    for row in events:
        if row.get("event") == "action_completed" and row.get("waypoint_index") == 0:
            row["host_monotonic_ns"] = 61*NS
            row["waypoint_index"] = 10
    (run/"logs/coordination_events.jsonl").write_text("".join(json.dumps(r)+"\n" for r in events))
    result = A.analyze(run, tmp_path/"offroute", scenario_path=scenario)
    assert result["physical_checks"]["verified"] == 0
    assert all(r["completed_fraction"] == 0 for r in result["mission_progress"].values())


def test_pre_admission_pose_dwell_does_not_prove_execution():
    completion = dict(uav_id=1, goal_id="waypoint0:uav1", source_sim_time_s=13,
        dwell_s=2, target_position_m=[10,10,12], target_frame="ENU", host_monotonic_ns=13*NS)
    poses = [dict(uav_id=1, session_id="unit", source_sim_time_s=t, sample_host_ns=t*NS,
                  quality="observed", x_m=10,y_m=10,z_m=12,speed_mps=0) for t in (11,11.5,12,12.5,13)]
    assert A.replay_dwell(completion, poses, {}, first_goal_admission_ns=12*NS)["status"] == "not_verified"


def test_complete_study_design_contains_unpooled_sensitivity_and_boundary_pairs():
    spec = importlib.util.spec_from_file_location("urban_prepare", ROOT/"scripts/product/prepare_urban_study.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    rows = prepare.design()
    assert len(rows) == len({r["case_id"] for r in rows}) == 56
    assert [sum(r["cohort"] == c for r in rows) for c in ("main","sensitivity","engineering_boundary")] == [40,12,4]
    assert len(prepare.design(5)) == 36


def new_goal_fixture(uavs=(1,)):
    events,commands,checks=[],[],[]
    for i in uavs:
        for label,born,finished in (("old",9,14),("new",11,17+i/10)):
            goal=f"{label}:uav{i}"
            target=(10.,float(i),12.)
            events.append(dict(event="goal_created",uav_id=i,goal_id=goal,goal_created_host_monotonic_ns=born*NS,
                host_monotonic_ns=born*NS,target_position_m=list(target),target_frame="ENU"))
            for suffix,created,at in (("first",born+.2,born+.4),("latest",finished-.3,finished-.2)):
                commands.append(dict(command_id=f"{goal}:{suffix}",uav_id=i,leader_id=2,epoch=2,
                    goal_id=goal,goal_created_ns=born*NS,created_ns=created*NS,admitted_ns=at*NS,
                    target_position_m=target,lease_status="valid",lineage_status="valid",
                    application_chain_complete=True,policy_allowed=True,lease_valid_until_ns=30*NS))
            commands.append(dict(command_id=f"{goal}:lost",uav_id=i,leader_id=2,epoch=2,goal_id=goal,
                goal_created_ns=born*NS,created_ns=(born+.1)*NS,admitted_ns=None,target_position_m=target,
                lease_status="no_admission",lineage_status="no_admission",application_chain_complete=False,policy_allowed=True))
            events.append(dict(event="action_completed",uav_id=i,goal_id=goal,goal_created_host_monotonic_ns=born*NS,
                host_monotonic_ns=finished*NS,admitted_command_id=f"{goal}:latest",admitted_epoch=2,leader_id=2,
                target_position_m=list(target),target_frame="ENU"))
            checks.append(dict(uav_id=i,goal_id=goal,completed_host_ns=finished*NS,status="verified",
                command_lineage_valid=True,configured_waypoint_valid=True))
    return events,commands,checks


def test_new_goal_endpoint_excludes_old_goal_and_separates_first_latest_CIDs():
    events,commands,checks=new_goal_fixture()
    result=A.new_goal_after_NLOS(events,commands,checks,10*NS,30*NS,full_window=True,data_valid=True)
    row=result["per_uav"]["1"]
    assert row["goal_id"]=="new:uav1" and abs(row["time_s"]-7.1)<1e-9
    assert row["first_admitted_goal_CID"]=="new:uav1:first"
    assert row["completion_refresh_CID"]=="new:uav1:latest"
    assert row["earliest_created_goal_CID"]=="new:uav1:lost" and row["earlier_created_unadmitted_CIDs"]==1
    assert row["chronology_verified"] and abs(row["sum_error_s"])<1e-9
    assert abs(sum(row["stage_decomposition"].values())-row["time_s"])<1e-9
    assert result["all_five"]["status"]=="administratively_right_censored"


def test_new_goal_group_is_maximum_of_verified_members_and_keeps_censor_crash_NA():
    events,commands,checks=new_goal_fixture(range(1,6))
    result=A.new_goal_after_NLOS(events,commands,checks,10*NS,30*NS,full_window=True,data_valid=True)
    assert result["all_five"]["critical_uav_id"]==5
    assert abs(result["all_five"]["time_s"]-7.5)<1e-9
    assert result["all_five"]["same_relay_epoch"] is True
    checks[:]=[r for r in checks if r["goal_id"]!="new:uav5"]
    result=A.new_goal_after_NLOS(events,commands,checks,10*NS,30*NS,full_window=True,data_valid=True)
    assert result["per_uav"]["5"]["status"]=="administratively_right_censored"
    result=A.new_goal_after_NLOS(events,commands,checks,10*NS,18*NS,full_window=False,data_valid=True)
    assert result["all_five"]["status"]=="interrupted_attempt"
    result=A.new_goal_after_NLOS(events,commands,checks,None,30*NS,full_window=True,data_valid=True)
    assert result["all_five"]["recovered"] is None and result["all_five"]["time_s"] is None
