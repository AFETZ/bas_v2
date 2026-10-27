"""Predeclared design and synthetic measurements; no simulator is launched."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location("sensitivity",ROOT/"scripts/product/relay_sensitivity.py")
S=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(S)
F=importlib.util.spec_from_file_location("fixtures",Path(__file__).with_name("test_relay_analysis.py"))
FIX=importlib.util.module_from_spec(F);F.loader.exec_module(FIX)
NS=1_000_000_000


def prepared(tmp_path):
    raw,_=FIX.fixture(tmp_path)
    result=FIX.analyze(raw,tmp_path)
    planned=next(r for r in S.plan() if r["policy"]=="failover" and float(r["outage_duration_s"])==.5)
    result["pilot"]=False
    meta=result["metadata"]
    meta["environment"].update(git_head="f"*40,source_dirty_paths="0",wifi_aggregation_requested="0",wifi_explicit_bar_after_missed_block_ack_requested="0")
    meta["native_stats"].update(rng_seed=1,rng_run=planned["rng_run"],wifi_aggregation_enabled=False,wifi_explicit_bar_after_missed_block_ack=False)
    meta["configuration_snapshot"]={"container_image_id":"sha256:fixture"}
    cfg=meta["config"]
    cfg.update({k:1 for k in S.A.MISSION_FIELDS})
    cfg.update(initial_master=1,experiment_profile="sensitivity",parameter_snapshots=True,parameter_workers=5,
        **{k:float(planned[k]) for k in ("duration_s","fault_start_s","fault_end_s")})
    cfg["effective_service_args"]={k:1 for k in S.A.SERVICE_FIELDS}
    return planned,result


def test_eighteen_plan_has_same_random_start_per_repeat_for_all_durations_and_policies():
    rows=S.plan();assert len(rows)==18 and rows==S.plan()
    S.validate_plan(rows)
    for repeat in (1,2,3):
        rr=[r for r in rows if r["repeat"]==repeat]
        assert len(rr)==6 and len({r["fault_start_s"] for r in rr})==1
        assert 40<=float(rr[0]["fault_start_s"])<=60
        assert {float(r["outage_duration_s"]) for r in rr}=={.5,2,10}
    assert len({r["fault_start_s"] for r in rows})==3
    for i in range(0,18,2):
        assert rows[i]["pair_id"]==rows[i+1]["pair_id"] and {rows[i]["policy"],rows[i+1]["policy"]}=={"fixed","failover"}
    specs=S.launch_specs(rows)
    assert all(r["argv"][:5]==["flock","--nonblock","--conflict-exit-code","75","/tmp/bas-paper-runtime.lock"] for r in specs)
    assert all(r["environment_overrides"]["BAS_NATIVE_EXPERIMENT_PROFILE"]=="sensitivity" and r["environment_overrides"]["BAS_NATIVE_EXPERIMENT_PILOT"]=="0" for r in specs)


def test_post_restoration_selection_and_activation_are_distinct_observations():
    events=[dict(event="grant_issued",host_monotonic_ns=90*NS,leader_id=1,epoch=1,reason="selection"),
        dict(event="grant_issued",host_monotonic_ns=99.7*NS,leader_id=1,epoch=1,reason="renewal"),
        dict(event="gcs_probe_issued",host_monotonic_ns=99.9*NS,destination_id=1,probe_sent_host_monotonic_ns=99.9*NS,send_ok=True),
        dict(event="gcs_probe_issued",host_monotonic_ns=99.95*NS,destination_id=1,probe_sent_host_monotonic_ns=99.95*NS,send_ok=False),
        dict(event="gcs_probe_echo_received",host_monotonic_ns=100.6*NS,uav_id=1,probe_sent_host_monotonic_ns=100.55*NS),
        dict(event="grant_issued",host_monotonic_ns=100.4*NS,leader_id=2,epoch=2,reason="selection"),
        dict(event="leader_activated",host_monotonic_ns=100.7*NS,leader_id=2,node_id=2,epoch=2),
        dict(event="leader_activated",host_monotonic_ns=100.65*NS,leader_id=2,node_id=3,epoch=2),
        dict(event="grant_issued",host_monotonic_ns=100.8*NS,leader_id=3,epoch=3,reason="selection"),
        dict(event="grant_issued",host_monotonic_ns=200*NS,leader_id=4,epoch=4,reason="selection"),
        dict(event="leader_activated",host_monotonic_ns=200.1*NS,leader_id=4,node_id=4,epoch=4)]
    r=S.timing_and_selections(events,[],100*NS,100.5*NS,1,measurement_start_ns=95*NS,measurement_end_ns=150*NS)
    assert r["new_relay_selections"]==2 and r["new_relay_own_activations"]==1
    assert r["selected_after_RFend"]==1 and r["activated_after_RFend"]==1
    assert r["selected_after_confirmed_old_channel"]==1 and r["activated_after_confirmed_old_channel"]==1
    assert r["previous_times"]["previous_probe_sent_host_ns"]==99.9*NS
    assert r["probe_local_send_failures"]==1
    assert r["previous_times"]["previous_renewal_issue_event_host_ns"]==99.7*NS
    assert r["outside_measurement_new_selections"]==1
    assert r["outside_measurement_first_activations"]==1


def test_common_recovery_without_a_new_relay_does_not_become_handover(tmp_path):
    planned,r=prepared(tmp_path)
    r["control_recovery"]["all_five"].update(recovered=True,time_s=1.5)
    r["handover_recovery"]["all_five"].update(recovered=False,time_s=None,censor_time_s=100)
    result=S.sensitivity_record(planned,r,[],[])
    assert result["network_eligible"] is True
    assert result["outcomes"]["common_UART_recovery_s"]==1.5
    assert result["outcomes"]["common_UART_by4s"]==1
    assert result["outcomes"]["handover_by4s_where_changed"] is None
    assert result["outcomes"]["handover_changed"]==0
    r["control_recovery"]["all_five"].update(recovered=True,time_s=5.2)
    result=S.sensitivity_record(planned,r,[],[])
    assert result["outcomes"]["common_UART_recovery_s"]==5.2
    assert result["outcomes"]["common_UART_over4_or_not_recovered"]==1
    assert S.outcome_by_four(dict(recovered=False,censor_time_s=3)) is None
    assert S.outcome_by_four(dict(recovered=False,censor_time_s=7))==0
    r["actual_fault"].update(start_host_monotonic_ns=None,end_host_monotonic_ns=None)
    unknown=S.sensitivity_record(planned,r,[],[])
    assert unknown["outcomes"]["handover_changed"] is None


def test_planned_windows_can_differ_but_profile_snapshot_or_unplanned_changes_cannot(tmp_path):
    planned,r=prepared(tmp_path)
    good=S.sensitivity_record(planned,r,[],[])
    other=next(x for x in S.plan() if x["policy"]=="failover" and x["rng_run"]==planned["rng_run"] and x["outage_duration_s"]!=planned["outage_duration_s"])
    changed=deepcopy(r)
    changed["metadata"]["config"].update({k:float(other[k]) for k in ("fault_start_s","fault_end_s")})
    assert S.sensitivity_record(other,changed,[],[])["comparability"]==good["comparability"]
    changed["metadata"]["config"]["fault_start_s"]+=.1
    assert not S.sensitivity_record(other,changed,[],[])["study_eligible"]
    changed=deepcopy(r);changed["metadata"]["config"]["experiment_profile"]="main"
    assert not S.sensitivity_record(planned,changed,[],[])["study_eligible"]
    changed=deepcopy(r);changed["metadata"]["config"]["parameter_snapshots"]=False
    assert S.sensitivity_record(planned,changed,[],[])["comparability"]!=good["comparability"]


def test_parameter_workers_is_an_explicit_study_factor_not_a_defaulted_observation(tmp_path):
    planned,r=prepared(tmp_path)
    good=S.sensitivity_record(planned,r,[],[])
    assert good["comparability"]["parameter_workers"]==5
    changed=deepcopy(r);changed["metadata"]["config"]["parameter_workers"]=1
    assert S.sensitivity_record(planned,changed,[],[])["comparability"]!=good["comparability"]
    for invalid in (None,True,0,6,1.0,"5"):
        changed=deepcopy(r);changed["metadata"]["config"]["parameter_workers"]=invalid
        checked=S.sensitivity_record(planned,changed,[],[])
        assert not checked["study_eligible"] and not checked["network_eligible"]
        assert any("parameter_workers" in issue for issue in checked["exclusion_reasons"])


def test_missing_runs_leave_every_pair_point_missing_not_zero(tmp_path):
    r=S.analyze(S.plan(),tmp_path/"missing",tmp_path/"out",False)
    assert len(r["records"])==18
    assert len({p["pair_id"] for p in r["paired_points"]})==9
    assert all(p["fixed"] is None and p["failover"] is None and p["difference"] is None for p in r["paired_points"])
    assert all(e["n_pairs"]==0 and e["mean_difference"] is None for e in r["paired_estimates"])


def test_freeze_matches_only_explicit_source_image_and_plan_bytes(tmp_path):
    path=tmp_path/"plan.csv";path.write_text("fixed bytes")
    freeze=dict(study_id=S.STUDY,frozen_head="abc",container_image_id="sha256:actual",
        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),base_environment={"BAS_NATIVE_PARAMETER_SNAPSHOTS":"1","BAS_NATIVE_PARAMETER_WORKERS":"5"})
    S.verify_freeze(freeze,path,"abc",False,"sha256:actual")
    for invalid in (None,0,6,"0","6","1.0"):
        changed=deepcopy(freeze);changed["base_environment"]["BAS_NATIVE_PARAMETER_WORKERS"]=invalid
        try:S.verify_freeze(changed,path,"abc",False,"sha256:actual")
        except ValueError:pass
        else:raise AssertionError("missing/invalid worker freeze was accepted")
    for head,dirty,image in (("changed",False,"sha256:actual"),("abc",True,"sha256:actual"),("abc",False,"sha256:other")):
        try:S.verify_freeze(freeze,path,head,dirty,image)
        except ValueError:pass
        else:raise AssertionError("changed root freeze was accepted")
    path.write_text("changed bytes")
    try:S.verify_freeze(freeze,path,"abc",False,"sha256:actual")
    except ValueError:pass
    else:raise AssertionError("changed plan was accepted")
