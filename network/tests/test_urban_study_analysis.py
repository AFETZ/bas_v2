"""Synthetic aggregation fixtures only; never results of integrated experiments."""
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location("urban_study",ROOT/"scripts/product/analyze_urban_study.py")
S=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)
NS=1_000_000_000


def test_restricted_loss_requires_full60s_and_does_not_impute_recovery():
    observed=dict(status="observed",time_s=3)
    assert S.bounded_endpoint(observed,0,59*NS,complete=False,recording_valid=True)["value"] is None
    assert S.bounded_endpoint(observed,0,60*NS,complete=False,recording_valid=True)["value"]==3
    censored=S.bounded_endpoint(dict(status="administratively_right_censored"),0,100*NS,complete=True,recording_valid=True)
    assert censored["value"]==60 and censored["observed_time_s"] is None
    assert censored["status"]=="right_censored_bounded_loss_known"
    assert S.bounded_endpoint(observed,None,100*NS,complete=True,recording_valid=True)["value"] is None
    assert S.bounded_endpoint(observed,0,100*NS,complete=True,recording_valid=False)["value"] is None


def test_whole_pair_bootstrap_has_exact5_and_deterministic10():
    exact=S.interval([2]*5)
    assert exact["resamples"]==3125 and exact["interval_95"]==[2,2]
    a=S.interval(list(range(10)),draws=200)
    b=S.interval(list(range(10)),draws=200)
    assert a==b and a["n"]==10 and a["resamples"]==200
    assert S.interval([2])["interval_95"] is None


def row(block,mode,policy,value,cohort="main"):
    return dict(run_id=f"{block}-{mode}-{policy}",block_id=block,radio_mode=mode,policy=policy,cohort=cohort,
        scenario_variant="nominal",parameter_variant="nominal",outcomes={"timely_per_offered":value})


def test_interaction_requires_complete_four_case_blocks():
    rows=[row("b1","geometry","fixed",.2),row("b1","geometry","failover",.8),
          row("b1","los_only","fixed",.9),row("b1","los_only","failover",.95),
          row("b2","geometry","fixed",None),row("b2","geometry","failover",.7),
          row("b2","los_only","fixed",.9),row("b2","los_only","failover",.9)]
    pairs,contrasts,interactions=S.contrasts(rows)
    item=next(r for r in interactions if r["metric"]=="timely_per_offered")
    assert item["planned_blocks"]==2 and item["complete_four_case_blocks"]==1
    assert abs(item["estimate"]["mean"]-.55)<1e-12
    assert item["estimate"]["interval_95"] is None
    assert item["missing_outcome_sensitivity"]["status"]=="deterministic_range_bounds_not_CI"
    assert len([r for r in pairs if r["metric"]=="timely_per_offered"])==4


def test_missing_outcome_bounds_are_not_complete_pair_confidence_intervals():
    bounds=S.missing_bounds([dict(fixed=.3,failover=None),dict(fixed=.5,failover=.7)],"timely_per_offered")
    assert all(abs(a-b)<1e-12 for a,b in zip(bounds["mean_difference_bounds"],[-.05,.45]))
    assert bounds["planned_pairs"]==2 and bounds["status"]=="deterministic_range_bounds"
    assert S.missing_bounds([dict(fixed=None,failover=None,not_applicable=True)],"restricted_post_NLOS_UART_60s")["status"].startswith("not_identified")


def test_auxiliary_small_cohorts_stay_descriptive():
    rows=[row(f"b{i}","geometry",p,.2 if p=="fixed" else .8,"sensitivity") for i in range(3) for p in ("fixed","failover")]
    _,summaries,_=S.contrasts(rows)
    item=next(r for r in summaries if r["metric"]=="timely_per_offered")
    assert item["complete_pairs"]==3 and item["failover_minus_fixed"]["interval_95"] is None
    assert item["failover_minus_fixed"]["method"]=="descriptive auxiliary cohort"


def fixture_case(root, name, *, complete=True, status="completed", pilot=False):
    case=dict(run_id=name,policy="failover",cohort="main",radio_mode="geometry",block_id="b1",
        scenario_variant="nominal",parameter_variant="nominal",rng_seed=1,rng_run=1,
        config_sha256="fixture-config",duration_s=300)
    frozen=dict(source_commit="fixture-source",image_id="fixture-image")
    result=dict(run_id=name,policy="failover",recorded_status=status,pilot=pilot,
        observation_window=dict(complete=complete,start_host_monotonic_ns=0,end_host_monotonic_ns=300*NS if complete else 30*NS),
        provenance=dict(scenario_sha256="fixture-config"),missing_measurements=[],data_quality_errors=[],
        rates=dict(all_five=dict(timely_per_offered=.75)),
        mission_progress={str(i):dict(planned_waypoints=5,completed_waypoints=3,route_finished=False) for i in range(1,6)},
        performance=dict(integral_RTF=.999),authority=dict(selections=1),
        trigger=dict(event=dict(host_monotonic_ns=10*NS)),uart_after_NLOS=dict(all_five=dict(status="observed",time_s=3)))
    common=dict(metadata=dict(environment=dict(git_head="fixture-source",source_dirty_paths="0"),
        native_stats=dict(rng_seed=1,rng_run=1,geometry_radio_mode="geometry",selected_propagation_profile="geometry_logdistance",fault_profile="none"),
        config=dict(fault_profile="none",duration_s=300),configuration_snapshot=dict(container_image_id="fixture-image")))
    ledger=dict(run_id=name,source_commit="fixture-source",config_sha256="fixture-config",returncode=0 if status=="completed" else 2)
    export=root/name
    (export/"metrics").mkdir(parents=True)
    (export/"relay_audit/metrics").mkdir(parents=True)
    (export/"metrics/urban_analysis.json").write_text(json.dumps(result))
    (export/"relay_audit/metrics/relay_analysis.json").write_text(json.dumps(common))
    controller=root/(name+".json");controller.write_text(json.dumps(ledger))
    return case,frozen,export,controller


def test_failed_cleanup_keeps_complete_window_but_crash_cannot_enter_primary(tmp_path):
    args=fixture_case(tmp_path,"cleanup",status="failed")
    r=S.run_record(*args[:2],args[2],args[3])
    assert r["attempt_failed"] and r["primary_eligible"]
    assert r["outcomes"]["timely_per_offered"]==.75
    args=fixture_case(tmp_path,"crash",complete=False,status="failed")
    r=S.run_record(*args[:2],args[2],args[3])
    assert r["attempt_failed"] and not r["primary_eligible"]
    assert all(value is None for value in r["outcomes"].values())
    assert r["restricted_post_NLOS"]["status"]=="interrupted_before_tau"


def test_provenance_mismatch_and_pilot_are_preserved_without_pooling(tmp_path):
    args=fixture_case(tmp_path,"pilot",pilot=True)
    r=S.run_record(*args[:2],args[2],args[3])
    assert r["attempted"] and not r["primary_eligible"]
    assert any("pilot" in reason for reason in r["provenance_errors"])
    args=fixture_case(tmp_path,"wrong")
    ledger=json.loads(args[3].read_text());ledger["source_commit"]="other-source";args[3].write_text(json.dumps(ledger))
    r=S.run_record(*args[:2],args[2],args[3])
    assert not r["primary_eligible"] and r["outcomes"]["timely_per_offered"] is None


def test_planned_unattempted_case_remains_row(tmp_path):
    case=dict(run_id="not-run",policy="fixed",radio_mode="geometry",block_id="b1")
    r=S.run_record(case,{},tmp_path/"absent",tmp_path/"absent.json")
    assert not r["attempted"] and not r["primary_eligible"]
    assert r["recorded_status"]=="not_run" and all(v is None for v in r["outcomes"].values())


def test_aggregate_preserves_missing_and_unplanned_attempts(tmp_path):
    exports=tmp_path/"exports";exports.mkdir()
    case,frozen,export,controller=fixture_case(exports,"recorded")
    missing=dict(case,run_id="not-run",policy="fixed")
    frozen["cases"]=[case,missing]
    freeze=tmp_path/"freeze.json";freeze.write_text(json.dumps(frozen))
    (exports/"extra.json").write_text(json.dumps(dict(run_id="unplanned-failed",returncode=2)))
    r=S.aggregate(freeze,exports,exports,tmp_path/"aggregate")
    assert r["planned_attempts"]==2 and r["observed_attempts"]==1
    assert len(r["runs"])==2 and len(r["unplanned_attempts"])==1
    metric=next(x for x in r["paired_contrasts"] if x["metric"]=="timely_per_offered")
    assert metric["complete_pairs"]==0 and metric["missing_outcome_sensitivity"]["planned_pairs"]==1
    assert (tmp_path/"aggregate/runs.csv").is_file()
