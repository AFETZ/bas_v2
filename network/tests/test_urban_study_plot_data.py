"""Small structural presentation checks; no graphics/runtime or result fixtures."""
import csv
import ast
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
        writer=csv.DictWriter(stream,fieldnames=["command_id","uav_id","epoch","leader_id","command_created_host_ns","follower_receive_host_ns","UART_write_end_host_ns"])
        writer.writeheader();writer.writerow(dict(command_id="2:1:1",uav_id=1,epoch=2,leader_id=2,command_created_host_ns=14*NS,
                                                   follower_receive_host_ns=14.1*NS,UART_write_end_host_ns=14.2*NS))
    (raw/"logs/coordination_uav1.jsonl").write_text(json.dumps(dict(event="leader_activated",epoch=2,leader_id=2,host_monotonic_ns=13.8*NS))+"\n")
    result=dict(trigger=dict(event=dict(host_monotonic_ns=12*NS,ns3_time_s=999)),
        uart_after_NLOS=dict(all_five=dict(command_ids={"1":"2:1:1"},epoch=2,leader_id=2),uav1=dict(event_host_ns=14.2*NS)))
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


def test_common_CID_critical_member_does_not_use_earlier_per_UAV_epochs(tmp_path):
    export=tmp_path/"export";(export/"metrics").mkdir(parents=True)
    raw=tmp_path/"raw";(raw/"logs").mkdir(parents=True)
    stages=[dict(command_id=f"3:{i}:1",uav_id=i,epoch=3,leader_id=4,
                 command_created_host_ns=14*NS,UART_write_end_host_ns=(19-i)*NS) for i in range(1,6)]
    with (export/"metrics/urban_target_CID_stages.csv").open("w") as stream:
        writer=csv.DictWriter(stream,fieldnames=list(stages[0]));writer.writeheader();writer.writerows(stages)
    (raw/"logs/coordination_uav1.jsonl").write_text(json.dumps(dict(event="leader_activated",epoch=3,leader_id=4,host_monotonic_ns=13*NS))+"\n")
    uart=dict(all_five=dict(command_ids={str(i):f"3:{i}:1" for i in range(1,6)},epoch=3,leader_id=4))
    # These individually first admissions can belong to older epochs and would
    # choose UAV2, whose common-epoch CID is NOT the last of the five.
    uart.update(uav1=dict(event_host_ns=11*NS,epoch=1),uav2=dict(event_host_ns=19*NS,epoch=2),
                **{f"uav{i}":dict(event_host_ns=12*NS,epoch=1) for i in (3,4,5)})
    data=dict(result=dict(trigger=dict(event=dict(host_monotonic_ns=12*NS)),uart_after_NLOS=uart),
              begin=10*NS,end=20*NS,gcs=[],raw=raw,export=export)
    timeline=P.chronology_data(data,P.Inputs())
    chosen=next(r for r in timeline if r["event"]=="UART")
    assert chosen["command_id"]=="3:1:1" and chosen["uav_id"]=="1" and chosen["elapsed_host_s"]==8
    assert (chosen["common_epoch"],chosen["common_leader_id"])==(3,4)


def test_requested_altitude_carries_known_ENU_intent_without_filling_physics():
    def goal(at,z,frame="ENU",uav=1):
        return dict(event="goal_created",uav_id=uav,host_monotonic_ns=at*NS,
                    target_position_m=[0,0,z],target_frame=frame)
    events=[goal(5,70),goal(9,-12,"NED"),goal(12,60),goal(13,20,uav=2),goal(25,12)]
    assert P.requested_altitude_series(events,1,10*NS,20*NS)==[(0,70),(2,60),(10,60)]
    assert P.requested_altitude_series([goal(12,60)],1,10*NS,20*NS)==[(2,60),(10,60)]
    assert P.requested_altitude_series([goal(5,-12,"NED")],1,10*NS,20*NS)==[]


def test_publication_size_font_floor_and_exact_physical_canvas():
    assert P.PUBLICATION_WIDTH_CM==13.9
    assert P.PUBLICATION_WIDTH_IN*P.PUBLICATION_DPI>=1642
    assert P.FONT_MAIN>=7 and P.FONT_SMALL>=7
    tree=ast.parse(Path(P.__file__).read_text())
    figures=[]
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg=="fontsize" and isinstance(keyword.value,ast.Constant):
                assert keyword.value.value>=7
            if keyword.arg=="figsize":
                assert isinstance(keyword.value,ast.Tuple)
                assert isinstance(keyword.value.elts[0],ast.Name) and keyword.value.elts[0].id=="PUBLICATION_WIDTH_IN"
                figures.append(node)
        if isinstance(node.func,ast.Attribute) and node.func.attr=="savefig":
            canvas=next(k.value for k in node.keywords if k.arg=="bbox_inches")
            assert isinstance(canvas,ast.Constant) and canvas.value is None
    assert len(figures)==4
