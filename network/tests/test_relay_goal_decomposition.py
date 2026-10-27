"""Synthetic lineage fixtures, not flight evidence."""
from copy import deepcopy
import csv
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("analysis_fixtures", Path(__file__).with_name("test_relay_analysis.py"))
FIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIX)
REPORT, NS = FIX.REPORT, FIX.NS


def commands_and_completions():
    commands, completed = [], []
    for i in range(1, 6):
        finish = 73 if i == 4 else 72 if i == 5 else 70 + i / 10
        first = 68.2 if i == 1 else 61 + i / 25
        target = (i, 0, 14)
        for name, at in (("first", first), ("latest", finish - .5)):
            commands.append(dict(command_id=f"{name}-{i}", uav_id=i, leader_id=4, epoch=2,
                goal_id=f"g:{i}", goal_created_ns=60*NS, created_ns=(at-.05)*NS, admitted_ns=at*NS,
                target_position_m=target, lease_status="valid", lineage_status="valid",
                application_chain_complete=True, policy_allowed=True, grant_sequence=1 if name=="first" else 2,
                lease_valid_from_ns=(at-.2)*NS, lease_valid_until_ns=(at+2)*NS))
        completed.append(dict(uav_id=i, leader_id=4, epoch=2, admitted_epoch=2, goal_id=f"g:{i}",
            goal_created_host_monotonic_ns=60*NS, host_monotonic_ns=finish*NS,
            target_position_m=list(target), target_frame="ENU", admitted_command_id=f"latest-{i}"))
    return commands, completed


def test_group_uses_last_finishing_uav_and_distinguishes_first_and_latest_cids():
    commands, completed = commands_and_completions()
    groups = REPORT.group_goal_decomposition(completed, commands, 55*NS)
    all5, peers = groups["all_five"], groups["four_followers"]
    assert all5["critical_uav_id"] == 4
    assert peers["critical_uav_id"] == 5
    assert peers["members"] == [1,2,3,5]
    assert all5["first_admitted_goal_CID"] == "first-4"
    assert all5["completion_refresh_CID"] == "latest-4"
    assert abs(all5["goal_creation_to_first_UART_s"] - 1.16) < 1e-8
    assert abs(all5["first_UART_to_completion_s"] - 11.84) < 1e-8
    assert all5["fault_to_completion_s"] == 18
    assert abs(all5["sum_error_s"]) < 1e-8
    latest = all5["latest_CID_chain"]
    assert latest["command_id"] == "latest-4"
    assert abs(latest["command_creation_to_UART_s"]-.05) < 1e-8
    assert abs(latest["UART_to_completion_s"]-.5) < 1e-8
    assert abs(latest["fault_to_command_creation_s"]+latest["command_creation_to_UART_s"]+latest["UART_to_completion_s"]-18) < 1e-8


def test_unknown_goal_metadata_or_mismatched_completion_epoch_is_unverifiable():
    commands, completed = commands_and_completions()
    unknown = deepcopy(commands[0]); unknown.update(goal_id=None, command_id="unknown-earlier")
    result = REPORT.decompose_goal(completed[0], commands+[unknown], 55*NS)
    assert result["status"] == "unverifiable_goal_lineage"
    assert "first_admitted_goal_CID" not in result
    wrong = deepcopy(completed[0]); wrong["admitted_epoch"] = 3
    assert REPORT.decompose_goal(wrong, commands, 55*NS)["status"] == "unverifiable_goal_lineage"
    expired = deepcopy(commands)
    next(r for r in expired if r["command_id"]=="latest-1")["lease_valid_until_ns"] = completed[0]["host_monotonic_ns"]-1
    assert REPORT.decompose_goal(completed[0], expired, 55*NS)["status"] == "unverifiable_goal_lineage"


def test_refresh_wait_is_separate_from_first_command_delivery_and_keeps_the_same_cid():
    commands,completed=commands_and_completions()
    first=next(row for row in commands if row["command_id"]=="first-4")
    first.update(created_ns=60_900_000_000,admitted_ns=60_903_000_000,
        lease_valid_from_ns=60_800_000_000,lease_valid_until_ns=62_903_000_000)
    result=REPORT.decompose_goal(next(row for row in completed if row["uav_id"]==4),commands,55*NS)
    assert result["status"]=="observed" and result["first_admitted_goal_CID"]=="first-4"
    assert result["completion_refresh_CID"]=="latest-4"
    assert result["first_command_created_host_ns"]==60_900_000_000
    assert result["goal_creation_to_first_command_creation_s"]==.9
    assert result["first_command_creation_to_UART_s"]==.003
    assert abs(result["goal_creation_to_first_UART_s"]-.903)<1e-12
    assert abs(result["goal_creation_to_first_command_creation_s"]+result["first_command_creation_to_UART_s"]-result["goal_creation_to_first_UART_s"])<1e-12
    assert result["fault_to_completion_s"]==18
    assert abs(result["fault_to_goal_creation_s"]+result["goal_creation_to_first_UART_s"]+result["first_UART_to_completion_s"]-18)<1e-12
    assert abs(result["latest_CID_chain"]["command_creation_to_UART_s"]-.05)<1e-8


def test_missing_former_master_does_not_become_a_fourth_follower():
    commands, completed = commands_and_completions()
    result = REPORT.group_goal_decomposition([r for r in completed if r["uav_id"] != 1], commands, 55*NS)
    assert result["all_five"]["status"] == "not_observed"
    assert result["four_followers"]["status"] == "not_observed"


def test_end_to_end_export_preserves_goal_and_completion_refresh_lineages(tmp_path):
    run, events = FIX.fixture(tmp_path)
    extra = []
    for row in events:
        if str(row.get("command_id", "")).startswith("2:"):
            row.update(goal_id=f"after-{row['uav_id']}", goal_created_host_monotonic_ns=57*NS)
        if row.get("event") in ("gcs_send","master_receive","master_forward","follower_receive","uart_admitted") and str(row.get("command_id", "")).startswith("2:"):
            earlier = deepcopy(row)
            delta = row["command_created_host_monotonic_ns"] - 57.5*NS
            earlier.update(command_id=f"early:{row['uav_id']}", payload_sha256=f"early-{row['uav_id']}")
            for key in ("host_monotonic_ns","command_created_host_monotonic_ns","admission_checked_host_monotonic_ns","uart_write_started_host_monotonic_ns","uart_write_end_host_monotonic_ns"):
                earlier[key] -= delta
            extra.append(earlier)
    (run / "logs/coordination_events.jsonl").write_text("".join(json.dumps(r)+"\n" for r in events+extra))
    result = FIX.analyze(run,tmp_path)
    group = result["goal_decomposition"]["all_five"]
    assert group["critical_uav_id"] == 5
    assert group["status"] == "observed"
    assert group["first_admitted_goal_CID"] == "early:5"
    assert group["completion_refresh_CID"] == "2:5:5"
    assert group["fault_to_completion_s"] == result["goal_recovery_all_five"]["time_s"] == 6
    assert "early:5" in (tmp_path / "out/metrics/relay_goal_decomposition.csv").read_text()
    with (tmp_path/"out/metrics/relay_goal_decomposition.csv").open() as stream:
        rows=list(csv.DictReader(stream))
    exported=next(row for row in rows if row["scope"]=="all_five")
    assert float(exported["first_command_created_host_ns"])==57.5*NS
    wait,delivery=(float(exported[key]) for key in ("goal_creation_to_first_command_creation_s","first_command_creation_to_UART_s"))
    assert wait==.5 and delivery<.041
    assert abs(wait+delivery-float(exported["goal_creation_to_first_UART_s"]))<1e-12
