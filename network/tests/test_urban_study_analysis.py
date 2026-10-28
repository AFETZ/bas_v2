"""Synthetic aggregation fixtures only; never results of integrated experiments."""
import importlib.util
import json
from copy import deepcopy
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


def test_crash_before_observed_trigger_has_unknown_applicability_not_full_window_NA():
    endpoint=S.bounded_endpoint({},None,70*NS,complete=False,recording_valid=False)
    assert endpoint["status"]=="interrupted_before_observed_trigger"
    assert endpoint["unknown_applicability"] is True and endpoint["value"] is None
    bounds=S.missing_bounds([dict(fixed=60,failover=None,unknown_applicability=True)],"restricted_post_NLOS_UART_60s")
    assert bounds["status"]=="not_identified_due_unknown_trigger_applicability"
    assert "mean_difference_bounds" not in bounds
    full=S.bounded_endpoint({},None,240*NS,complete=True,recording_valid=True)
    assert full["status"]=="not_applicable_no_trigger" and not full.get("unknown_applicability")


def test_reviewed_endpoint_scope_preserves_original_label_and_never_changes_observed_event():
    original=dict(status="not_applicable_no_direct_probe_detection",time_s=None)
    reviewed=S.interrupted_endpoint(original,complete=False,trigger=None,origin="direct_GCS_probe_timeout")
    assert original["status"]=="not_applicable_no_direct_probe_detection"
    assert reviewed["original_status"]==original["status"] and reviewed["unknown_applicability"] is True
    assert reviewed["status"]=="interrupted_before_observed_trigger"
    assert S.interrupted_endpoint(original,complete=True,trigger=None,origin="direct")==original
    observed=dict(status="observed",time_s=3,event_host_ns=3*NS)
    assert S.interrupted_endpoint(observed,complete=False,trigger=0,origin="direct")==observed


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
    frozen=dict(source_commit="fixture-source",image_id="fixture-image",native_binary=dict(sha256="fixture-native"))
    result=dict(run_id=name,policy="failover",recorded_status=status,pilot=pilot,
        observation_window=dict(complete=complete,start_host_monotonic_ns=0,end_host_monotonic_ns=300*NS if complete else 30*NS),
        provenance=dict(scenario_sha256="fixture-config"),missing_measurements=[],data_quality_errors=[],
        rates=dict(all_five=dict(timely_per_offered=.75)),
        mission_progress={str(i):dict(planned_waypoints=5,completed_waypoints=3,route_finished=False) for i in range(1,6)},
        performance=dict(integral_RTF=.999),authority=dict(selections=1),
        trigger=dict(event=dict(host_monotonic_ns=10*NS)),uart_after_NLOS=dict(all_five=dict(status="observed",time_s=3)))
    common=dict(metadata=dict(environment=dict(git_head="fixture-source",source_dirty_paths="0",native_binary_sha256="fixture-native"),
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
    frozen["cases"]=[case,missing,dict(case,run_id="los-fixed",policy="fixed",radio_mode="los_only"),
                     dict(case,run_id="los-failover",radio_mode="los_only")]
    freeze=tmp_path/"freeze.json";freeze.write_text(json.dumps(frozen))
    (exports/"extra.json").write_text(json.dumps(dict(run_id="unplanned-failed",returncode=2)))
    r=S.aggregate(freeze,exports,exports,tmp_path/"aggregate")
    assert r["planned_attempts"]==4 and r["observed_attempts"]==1
    assert len(r["runs"])==4 and len(r["unplanned_attempts"])==1
    metric=next(x for x in r["paired_contrasts"] if x["metric"]=="timely_per_offered")
    assert metric["complete_pairs"]==0 and metric["missing_outcome_sensitivity"]["planned_pairs"]==1
    assert (tmp_path/"aggregate/runs.csv").is_file()


def frozen_block():
    return dict(source_commit="fixture-source",image_id="fixture-image",native_binary=dict(sha256="fixture-native"),cases=[
        dict(run_id=f"unit-{mode}-{policy}",block_id="b1",cohort="main",policy=policy,radio_mode=mode,
             scenario_variant="nominal",parameter_variant="nominal",rng_seed=1,rng_run=7,duration_s=300,
             config_sha256=f"fixture-{mode}")
        for mode in ("geometry","los_only") for policy in ("fixed","failover")])


def rejected(frozen, fragment):
    try:
        S.validate_frozen_blocks(frozen)
    except ValueError as error:
        assert fragment in str(error)
    else:
        raise AssertionError("malformed freeze was accepted")


def test_frozen_main_blocks_reject_wrong_seed_duplicate_and_missing_cells():
    valid=frozen_block()
    assert len(S.validate_frozen_blocks(valid))==4
    for field in ("rng_seed","rng_run","duration_s","scenario_variant","parameter_variant"):
        wrong=deepcopy(valid)
        wrong["cases"][-1][field]=2 if field in ("rng_seed","rng_run") else 200 if field=="duration_s" else "other"
        rejected(wrong,f"mixes {field}")
    duplicate=deepcopy(valid)
    duplicate["cases"][-1].update(policy="fixed",radio_mode="geometry")
    rejected(duplicate,"duplicate frozen policy/radio cell")
    missing=deepcopy(valid);missing["cases"].pop()
    rejected(missing,"exactly four factorial cells")


def test_frozen_pairs_reject_mixed_config_source_and_image():
    for field in ("source_commit","image_id"):
        wrong=frozen_block();wrong["cases"][0][field]="different"
        rejected(wrong,f"case {field} differs")
    wrong=frozen_block();wrong["cases"][0]["config_sha256"]="different"
    rejected(wrong,"fixed/failover config differs")


def test_frozen_auxiliary_blocks_require_exact_matching_pairs():
    frozen=frozen_block();frozen["cases"]=frozen["cases"][:2]
    for row in frozen["cases"]:
        row["cohort"]="sensitivity"
    assert len(S.validate_frozen_blocks(frozen))==2
    wrong=deepcopy(frozen);wrong["cases"][1]["rng_run"]=8
    rejected(wrong,"mixes rng_run")
    wrong=deepcopy(frozen);wrong["cases"][1]["radio_mode"]="los_only"
    rejected(wrong,"one exact fixed/failover pair")
    wrong=deepcopy(frozen);wrong["cases"].pop()
    rejected(wrong,"one exact fixed/failover pair")


def test_interactions_do_not_pool_scenario_or_parameter_variants():
    rows=[]
    for variant,delta in (("nominal",.2),("other",.4)):
        for mode in ("geometry","los_only"):
            for policy in ("fixed","failover"):
                r=row("same-label",mode,policy,.5+(delta if mode=="geometry" and policy=="failover" else 0))
                r["scenario_variant"]=variant
                rows.append(r)
    _,_,interactions=S.contrasts(rows)
    items=[r for r in interactions if r["metric"]=="timely_per_offered"]
    assert len(items)==2 and all(r["complete_four_case_blocks"]==1 for r in items)
    assert {r["scenario_variant"] for r in items}=={"nominal","other"}


def test_main_native_executable_identity_missing_or_changed_cannot_be_pooled(tmp_path):
    frozen=frozen_block();frozen.pop("native_binary")
    rejected(frozen,"post-build native_binary.sha256")
    for label,value in (("absent",None),("changed","different-native")):
        case,frozen,export,controller=fixture_case(tmp_path,label)
        path=export/"relay_audit/metrics/relay_analysis.json"
        common=json.loads(path.read_text())
        if value is None:
            common["metadata"]["environment"].pop("native_binary_sha256")
        else:
            common["metadata"]["environment"]["native_binary_sha256"]=value
        path.write_text(json.dumps(common))
        result=S.run_record(case,frozen,export,controller)
        assert result["attempted"] and not result["primary_eligible"]
        assert any("executable digest" in error for error in result["provenance_errors"])


def test_missing_final_radio_stats_are_not_a_measured_model_mismatch(tmp_path):
    case,frozen,export,controller=fixture_case(tmp_path,"missing-stats",complete=False,status="failed")
    path=export/"relay_audit/metrics/relay_analysis.json"
    common=json.loads(path.read_text());common["metadata"]["native_stats"]={};path.write_text(json.dumps(common))
    result=S.run_record(case,frozen,export,controller)
    assert "effective geometry propagation/mode not recorded in final native stats" in result["provenance_errors"]
    assert "effective geometry propagation/mode differs" not in result["provenance_errors"]
