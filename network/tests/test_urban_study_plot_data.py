"""Small structural presentation checks; no graphics/runtime or result fixtures."""
import csv
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location("urban_study_plots",ROOT/"scripts/product/plot_urban_study.py")
P=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
NS=1_000_000_000


def freeze_fixture():
    rows=[]
    for block in ("r03","r01"):
        for mode in ("los_only","geometry"):
            for policy in ("failover","fixed"):
                rows.append(dict(order=len(rows)+1,cohort="main",block_id=block,radio_mode=mode,policy=policy,
                    scenario_variant="nominal",parameter_variant="nominal",run_id=f"{block}-{mode}-{policy}"))
    return dict(cases=rows)


def test_first_pair_uses_frozen_order_even_if_later_pair_is_available():
    pair=P.first_geometry_pair(freeze_fixture())
    assert set(pair)=={"fixed","failover"}
    assert {r["block_id"] for r in pair.values()}=={"r03"}
    assert all(r["radio_mode"]=="geometry" for r in pair.values())


def test_missing_outcome_is_not_zero_and_failed_observation_keeps_marker_status():
    frozen=freeze_fixture()
    aggregate=dict(runs=[dict(run_id="r03-geometry-fixed",attempt_failed=True,outcomes=dict(timely_per_offered=.4))])
    pairs=P.primary_points(aggregate,frozen,"timely_per_offered","geometry")
    first=pairs[("nominal","nominal","r03")]
    assert first["fixed"]["value"]==.4 and first["fixed"]["failed"]
    assert first["failover"]["value"] is None
    assert pairs[("nominal","nominal","r01")]["fixed"]["value"] is None


def test_trajectory_gaps_stale_and_clock_reset_are_not_connected():
    def point(t,quality="observed",session="a"):
        return dict(uav_id="1",source_sim_time_s=str(t),host_monotonic_ns=str((10+t)*NS),
                    quality=quality,session_id=session,x_m="1",y_m="2",z_m="3")
    rows=[point(0),point(.2),point(.3,"stale_or_fault"),point(.4),point(1.2),point(1.4),point(0,session="b")]
    segments=P.trajectory_segments(rows,1)
    assert [len(s) for s in segments]==[2,1,2,1]


def test_chronology_uses_host_clock_not_ns3_time_and_filters_old_selection(tmp_path):
    export=tmp_path/"export";(export/"metrics").mkdir(parents=True)
    raw=tmp_path/"raw";(raw/"logs").mkdir(parents=True)
    with (export/"metrics/urban_target_CID_stages.csv").open("w") as stream:
        writer=csv.DictWriter(stream,fieldnames=["command_id","command_created_host_ns","follower_receive_host_ns","UART_write_end_host_ns"])
        writer.writeheader();writer.writerow(dict(command_id="2:1:1",command_created_host_ns=14*NS,
                                                   follower_receive_host_ns=14.1*NS,UART_write_end_host_ns=14.2*NS))
    (raw/"logs/coordination_uav1.jsonl").write_text(json.dumps(dict(event="leader_activated",epoch=2,host_monotonic_ns=13.8*NS))+"\n")
    result=dict(trigger=dict(event=dict(host_monotonic_ns=12*NS,ns3_time_s=999)),
        uart_after_NLOS=dict(all_five=dict(command_ids={"1":"2:1:1"},epoch=2),uav1=dict(event_host_ns=14.2*NS)))
    gcs=[dict(event="grant_issued",reason="selection",host_monotonic_ns=9*NS,valid_from_ns=9*NS),
         dict(event="grant_issued",reason="selection",host_monotonic_ns=13*NS,valid_from_ns=13.5*NS,epoch=2)]
    data=dict(result=result,begin=10*NS,end=20*NS,gcs=gcs,raw=raw,export=export)
    timeline=P.chronology_data(data,P.Inputs())
    assert next(r for r in timeline if r["event"]=="NLOS")["elapsed_host_s"]==2
    assert [r["elapsed_host_s"] for r in timeline if r["event"]=="selection"]==[3]
    assert next(r for r in timeline if r["event"]=="UART")["elapsed_host_s"]==4.2


def test_all_four_caption_slots_are_explicit_about_evidence_scope():
    assert set(P.CAPTIONS)=={"urban_first_pair","urban_primary_effects","urban_chronology","urban_sensitivity_resources"}
    assert all(len(v)==2 and all(v) for v in P.CAPTIONS.values())
    assert "missing" in P.CAPTIONS["urban_primary_effects"][1].lower()
    assert "not uart" in P.CAPTIONS["urban_first_pair"][1].lower()


def test_running_last_attempt_cannot_be_mistaken_for_finished_batch():
    assert not P.batch_complete(dict(planned_attempts=2,observed_attempts=2,runs=[
        dict(controller_returncode=0),dict(controller_returncode=None)]))
    assert P.batch_complete(dict(planned_attempts=2,observed_attempts=2,runs=[
        dict(controller_returncode=0),dict(controller_returncode=2)]))
