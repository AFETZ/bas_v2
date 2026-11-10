"""CPU-only service overlay regressions. Fixtures are not field measurements."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from network.coordination.payload import normalize_payload_load, payload_load_slots, payload_load_slot_ns
from scripts.product import compute_radio_maps as maps


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def lineage(seconds=60):
    profile = normalize_payload_load({"class": "10kbps", "uav_ids": [1]}, (1,))
    start, end = 10**9, round((1+seconds)*1e9)
    records = []
    for slot in range(payload_load_slots(profile, end-start)):
        created = payload_load_slot_ns(profile, start, slot)
        records.append(dict(frame_uid=f"1:{slot}", load_id="payload_load", uav_id=1,
            direction="gcs_to_uav", process_epoch="gcs:1", created_host_monotonic_ns=created,
            accepted_host_monotonic_ns=created+1_000_000,
            target_observation_end_host_monotonic_ns=end+10**10,
            application_useful_bytes=profile["useful_bytes"], outcome="timely"))
    return dict(measurement_eligible=True, records=records,
        trace_files=[dict(path="unit-fixture.jsonl",sha256="a"*64,bytes=123,status="observed")],
        offered_load=dict(config_sha256="b"*64,
        status="recorded", generator_eligible=True, profile=profile,
        load_window=dict(start_host_monotonic_ns=start, end_host_monotonic_ns=end)))


class ServiceMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root/"rf"
        self.snapshot.mkdir()
        origin = dict(origin_latitude_deg=1, origin_longitude_deg=2,
                      origin_elevation_m=3, spherical_heading_deg=0)
        self.scene = b"<scene/>"
        scene_sha = hashlib.sha256(self.scene).hexdigest()
        self.metadata = dict(status="completed", origin=origin,
            inputs=dict(scene_xml=dict(sha256=scene_sha), radio_config=dict(sha256="a"*64),
                        **{"scene/scene.xml": dict(sha256=scene_sha)}),
            map=dict(center_m=[20,0,10],size_m=[20,20],cell_size_m=[10,10],time_s=15),
            desired=dict(id="cp",position_m=[0,0,2],pattern="iso",gain_dbi=0,power_w=.01,
                         center_hz=2412000000,bandwidth_hz=20000000),
            receiver=dict(center_hz=2412000000,bandwidth_hz=20000000),
            source_states=[])
        write_json(self.snapshot/"metadata.json", self.metadata)
        np.savez_compressed(self.snapshot/"maps.npz",cell_centers_enu_m=maps.grid_centers(self.metadata["map"]))
        self.evidence = dict(schema_version=1,
            snapshot_metadata_sha256=maps._stream_sha(self.snapshot/"metadata.json"),
            snapshot_npz_sha256=maps._stream_sha(self.snapshot/"maps.npz"),
            uav_count=1,target_uav_id=1,direction="gcs_to_uav",peer_positions_m={"cp":[0,0,2]},
            position_tolerance_m=[1,1,.5],bindings=[dict(row=0,column=0,run_id="fixture")])
        native = self.root/"runs/native-radio-realtime/fixture"
        write_json(native/"configuration/final/inputs/geometry_source/value.json", dict(frame=origin))
        write_json(native/"configuration/final/inputs/sionna_geometry_manifest/value.json",
                   dict(files={"scene.xml":dict(sha256=scene_sha)}))
        xml = native/"configuration/final/inputs/sionna_scene/value.xml"
        xml.parent.mkdir(parents=True)
        xml.write_bytes(self.scene)
        self.observed = dict(native=native,meta=dict(radio_sha256="a"*64,source_commit="b"*40),
            stats=dict(uav_count=1,sionna_solver_enabled=True,sionna_drives_rx_psd=True,
                tx_power_w=.01,wifi_actual_center_frequency_hz=2412000000,wifi_channel_width_mhz=20),
            lineage=lineage(),capture_sha256="c"*64,timing=dict(eligible=True),
            pose={name:dict(samples=6000,gap_max_ns=10_000_000,minimum=np.array(point,dtype=float),
                maximum=np.array(point,dtype=float),native_min_s=10,native_max_s=70)
                for name,point in {"cp":[0,0,2],"uav1":[15,-5,10]}.items()})
        self.counter = 0

    def save_snapshot(self):
        write_json(self.snapshot/"metadata.json",self.metadata)
        self.evidence["snapshot_metadata_sha256"] = maps._stream_sha(self.snapshot/"metadata.json")

    def export(self, plot=False):
        self.counter += 1
        output = self.root/f"overlay{self.counter}"
        with patch.object(maps,"_service_run",return_value=self.observed):
            result = maps.service_maps(self.snapshot,self.evidence,output,root=self.root,plot=plot)
        with np.load(output/"service_maps.npz") as stored:
            status = np.array(stored["status"],copy=True)
        return result,status,output

    def test_only_actual_offered_class_and_cell_are_colored_not_other_rates_or_area(self):
        before = maps._stream_sha(self.snapshot/"maps.npz")
        result,status,_ = self.export()
        idx = result["class_order"].index("10kbps")
        self.assertEqual(status[idx,0,0],maps.SERVICE_STATUS["measured_pass"])
        self.assertEqual(np.count_nonzero(status),1)
        self.assertEqual(maps._stream_sha(self.snapshot/"maps.npz"),before)
        self.assertIn("not_evaluated",result["rf_snapshot_equivalence"])
        self.assertEqual(result["independent_series_status"],"insufficient_evidence")
        self.assertIn("synthetic",result["semantic_scope"])

    def test_empty_evidence_is_unknown_even_with_finite_rf_data(self):
        self.evidence["bindings"] = []
        result,status,output = self.export(plot=True)
        self.assertFalse(status.any())
        self.assertEqual(result["observations"],[])
        self.assertTrue((output/"service_maps.png").is_file())

    def test_twenty_seconds_and_censored_tail_never_become_pass(self):
        for mode in ("short","censored"):
            with self.subTest(mode=mode):
                self.observed["lineage"] = lineage(20 if mode=="short" else 60)
                if mode=="censored":
                    first = self.observed["lineage"]["records"][0]
                    first.update(accepted_host_monotonic_ns=None,outcome="missing",
                        target_observation_end_host_monotonic_ns=first["created_host_monotonic_ns"]+100)
                result,status,_ = self.export()
                self.assertEqual(result["observations"][0]["status"],"insufficient_evidence")
                self.assertNotIn(maps.SERVICE_STATUS["measured_pass"],status)

    def test_packet_loss_and_generator_failure_remain_distinct(self):
        for mode,expected in (("loss","measured_fail"),("generator","invalid")):
            with self.subTest(mode=mode):
                self.observed["lineage"] = lineage()
                if mode=="loss":
                    for record in self.observed["lineage"]["records"][:2]:
                        record.update(accepted_host_monotonic_ns=None,outcome="missing")
                else:
                    self.observed["lineage"]["offered_load"]["generator_eligible"] = False
                result,status,_ = self.export()
                self.assertEqual(result["observations"][0]["status"],expected)
                self.assertEqual(status[result["class_order"].index("10kbps"),0,0],maps.SERVICE_STATUS[expected])

    def test_pose_envelope_height_and_sampling_gaps_cannot_reuse_other_location(self):
        for field,value in (("maximum",np.array([15,-5,11.])),("minimum",np.array([13,-5,10.])),
                            ("gap_max_ns",100_000_001),("samples",1)):
            original = copy.deepcopy(self.observed["pose"]["uav1"])
            with self.subTest(field=field):
                self.observed["pose"]["uav1"][field] = value
                result,status,_ = self.export()
                self.assertEqual(result["observations"][0]["status"],"invalid")
                self.assertNotIn(maps.SERVICE_STATUS["measured_pass"],status)
            self.observed["pose"]["uav1"] = original

    def test_roster_radio_and_geometry_are_bound_to_actual_context(self):
        mutations = [
            lambda: self.observed["stats"].update(uav_count=2),
            lambda: self.observed["stats"].update(sionna_drives_rx_psd=False),
            lambda: self.observed["meta"].update(radio_sha256="f"*64),
            lambda: self.observed["stats"].update(tx_power_w=1),
            lambda: self.observed["stats"].update(wifi_actual_center_frequency_hz=2422000000),
            lambda: write_json(self.observed["native"]/"configuration/final/inputs/geometry_source/value.json",
                              dict(frame=dict(self.metadata["origin"],origin_elevation_m=4))),
        ]
        original = copy.deepcopy(self.observed)
        for mutation in mutations:
            self.observed = copy.deepcopy(original)
            with self.subTest(mutation=mutation):
                mutation()
                result,status,_ = self.export()
                self.assertEqual(result["observations"][0]["status"],"invalid")
                self.assertNotIn(maps.SERVICE_STATUS["measured_pass"],status)

    def test_snapshot_tamper_reverse_direction_unknown_parameters_and_duplicate_bindings_rejected(self):
        for mutation in (
            lambda e:e.update(snapshot_npz_sha256="0"*64),
            lambda e:e.update(direction="uav_to_gcs"),
            lambda e:e.update(ignored=True),
            lambda e:e.update(target_uav_id=True),
            lambda e:e.update(position_tolerance_m=[6,1,.5]),
            lambda e:e["bindings"].append(copy.deepcopy(e["bindings"][0])),
            lambda e:e["bindings"][0].update(column=-1),
        ):
            original = copy.deepcopy(self.evidence)
            with self.subTest(mutation=mutation):
                mutation(self.evidence)
                with self.assertRaises(ValueError):
                    self.export()
            self.evidence = original

    def test_source_pulse_must_not_change_during_measured_window(self):
        segment = dict(start_s=0,stop_s=100,period_s=.01,duty_cycle=.5)
        self.metadata["source_states"] = [dict(segment=segment,active=True)]
        self.save_snapshot()
        result,status,_ = self.export()
        self.assertEqual(result["observations"][0]["status"],"invalid")
        self.assertIn("source activity",result["observations"][0]["errors"][0])
        self.assertNotIn(maps.SERVICE_STATUS["measured_pass"],status)

    def test_schedule_check_handles_on_off_pulses_half_open_boundaries_and_rounding(self):
        segment = dict(start_s=10,stop_s=20,period_s=.01,duty_cycle=.25)
        for start,end,state,expected in (
                (10,10.0025,True,True),(10,10.002500001,True,False),
                (10.0025,10.01,False,True),(10.0025,10.010000001,False,False),
                (9,10,False,True),(20,21,False,True),(9,21,False,False),
                (10.0025,10.0026,True,False)):
            with self.subTest(start=start,end=end,state=state):
                self.assertEqual(maps._constant_source_state(segment,start,end,state),expected)

    def test_failing_observation_is_not_hidden_by_repeated_good_run(self):
        self.evidence["bindings"].append(dict(row=0,column=0,run_id="fixture-bad"))
        failed = copy.deepcopy(self.observed)
        for record in failed["lineage"]["records"][:2]:
            record.update(accepted_host_monotonic_ns=None,outcome="missing")
        with patch.object(maps,"_service_run",side_effect=[self.observed,failed]):
            output = self.root/"two-runs"
            result = maps.service_maps(self.snapshot,self.evidence,output,root=self.root)
        with np.load(output/"service_maps.npz") as data:
            self.assertEqual(data["status"][result["class_order"].index("10kbps"),0,0],
                             maps.SERVICE_STATUS["measured_fail"])
            self.assertEqual(data["observed_bindings"][result["class_order"].index("10kbps"),0,0],2)

    def test_raw_auditor_rejects_dirty_unclosed_runs_and_malformed_pose_without_mock_pass(self):
        from scripts.product import analyze_relay_experiment as analyzer
        from scripts.product import summarize_native_radio_five_uav as summary
        control = self.root/"runs/control/fixture/run.json"
        meta = dict(run_id="fixture",measurement_validity="valid",runtime_status="completed",
                    source_dirty=False,runtime_verification_errors=[],
                    node_state_journal_verification=dict(passed=True),
                    native_rt_worker_verification=dict(passed=True))
        mission = dict(measurement_start_host_monotonic_ns=10**9,
                       measurement_end_host_monotonic_ns=61*10**9,measurement_duration_wall_s=60)
        native = self.observed["native"]
        write_json(native/"metrics/relay_experiment_summary.json",mission)
        write_json(native/"metrics/native_radio_stats.json",self.observed["stats"])
        capture = native/"logs/native_radio_events.csv"
        capture.parent.mkdir(parents=True)
        capture.write_text("full raw capture retained")
        write_json(control,dict(meta,source_dirty=True))
        with self.assertRaisesRegex(ValueError,"unchanged"):
            maps._service_run("fixture",self.root)
        write_json(control,dict(meta,node_state_journal_verification=dict(passed=False)))
        with self.assertRaisesRegex(ValueError,"closed"):
            maps._service_run("fixture",self.root)
        write_json(control,meta)
        row = dict(event="live_pose",wall_monotonic_ns=2*10**9,node="uav1",time_s=2,
                   x=15,y=-5,z=10,value=100.001)
        with patch.object(summary,"iter_native_events",return_value=iter([row])):
            with self.assertRaisesRegex(ValueError,"pose age"):
                maps._service_run("fixture",self.root)
        with patch.object(summary,"iter_native_events",return_value=iter([])),              patch.object(analyzer,"analyze_payload_lineage",return_value=lineage()):
            observed = maps._service_run("fixture",self.root)
        self.assertFalse(observed["timing"]["eligible"])
        self.assertFalse(observed["lineage"]["measurement_eligible"])
        self.assertEqual(observed["capture_sha256"],maps._stream_sha(capture))


if __name__ == "__main__":
    unittest.main()
