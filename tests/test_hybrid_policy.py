"""Evidence adapters; native propagation/cache contracts are tested in C++."""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.product import hybrid_policy as policy


class HybridPolicyTests(unittest.TestCase):
    def test_scene_is_classified_from_actual_loaded_shapes_and_not_coordinates(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"scene.xml"
            path.write_text('<scene version="2.1.0"/>')
            def check(shapes,objects,xml=None):
                if xml is not None: path.write_text(xml)
                scene=SimpleNamespace(mi_scene=SimpleNamespace(shapes=lambda:list(range(shapes))),
                                      objects={str(i):i for i in range(objects)})
                with patch.dict("sys.modules",{"sionna.rt":SimpleNamespace(load_scene=lambda *a,**kw:scene)}), patch.object(policy,"version",side_effect=lambda name:{"sionna-rt":"1.2.0","mitsuba":"3.7.1","drjit":"1.2.0"}[name]):
                    return policy.scene_evidence(path)
            self.assertTrue(check(0,0)["qualified_empty_scene"])
            self.assertFalse(check(1,1)["qualified_empty_scene"])
            self.assertFalse(check(0,0,'<scene version="2.1.0"><include filename="other.xml"/></scene>')["qualified_empty_scene"])
            self.assertFalse(check(0,0,'<scene version="2.1.0"><shape type="obj"><string name="filename" value="mesh.obj"/></shape></scene>')["qualified_empty_scene"])

    def test_missing_rt_changed_input_and_wrong_version_fail_instead_of_friis(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"scene.xml";path.write_text('<scene version="2.1.0"/>')
            def changed(*args,**kwargs):
                path.write_text('<scene version="2.1.0"><shape type="rectangle"/></scene>')
                return SimpleNamespace(mi_scene=SimpleNamespace(shapes=lambda:[]),objects={})
            versions=lambda name:{"sionna-rt":"1.2.0","mitsuba":"3.7.1","drjit":"1.2.0"}[name]
            with patch.dict("sys.modules",{"sionna.rt":SimpleNamespace(load_scene=changed)}), patch.object(policy,"version",side_effect=versions):
                with self.assertRaisesRegex(ValueError,"changed"):
                    policy.scene_evidence(path)
            with patch.object(policy,"version",return_value="9.9.9"):
                with self.assertRaisesRegex(ValueError,"pinned"):
                    policy.scene_evidence(path)
            with patch.dict("sys.modules",{"sionna.rt":SimpleNamespace()}),patch.object(policy,"version",side_effect=versions):
                with self.assertRaises(ImportError):
                    policy.scene_evidence(path)

    def fixture(self,rt=0,friis=1):
        return dict(selected_propagation_profile="hybrid",sionna_drives_rx_psd=rt>0,
            state_max_age_s=.1,channel_state_max_age_s=.1,sionna_rt_execution_mode="worker_pool",
            rx_psd_propagation_model="bas::QualifiedSionnaFriisSelection",
            hybrid_selection=dict(policy_id=policy.POLICY_ID,rt_producer_retained=True,
                scene_sha256="a"*64,qualified_empty_scene=True,actual_shape_count=0,actual_object_count=0,
                rt_psd_evaluations=rt,friis_psd_evaluations=friis,guarded_friis_evaluations=friis,
                reference_guard_failures=0,links=[dict(tx_node_id=0,rx_node_id=1,
                    rt_psd_evaluations=rt,friis_psd_evaluations=friis,
                    last_model="sionna" if rt else "friis",
                    last_reason="unqualified_geometry" if rt else "qualified_empty_static_siso_psd")]))

    def test_both_measured_selection_modes_require_consistent_link_and_guard_counts(self):
        for rt,friis in [(1,0),(0,1),(3,5)]:
            self.assertTrue(policy.native_dispatch_valid(self.fixture(rt,friis)))
        for field,value in [("rt_psd_evaluations",True),("guarded_friis_evaluations",0),
                            ("reference_guard_failures",1),("rt_producer_retained",False),
                            ("links",[]),("friis_psd_evaluations",999)]:
            row=self.fixture();row["hybrid_selection"][field]=value
            with self.subTest(field=field):self.assertFalse(policy.native_dispatch_valid(row))
        for reason in ("unqualified_orientation","reference_not_single_nonzero_path"):
            row=self.fixture(rt=1,friis=0)
            row["hybrid_selection"]["links"][0]["last_reason"]=reason
            self.assertTrue(policy.native_dispatch_valid(row))
        row=self.fixture();row["hybrid_selection"]["links"]*=2
        self.assertFalse(policy.native_dispatch_valid(row))
        row=self.fixture();row["sionna_drives_rx_psd"]=True
        self.assertFalse(policy.native_dispatch_valid(row))
        row=self.fixture();row["hybrid_selection"]["links"][0]["last_reason"]="unqualified_geometry"
        self.assertFalse(policy.native_dispatch_valid(row))

    def ground_fixture(self, rt=0, friis=1):
        row = self.fixture(rt, friis)
        hybrid = row["hybrid_selection"]
        hybrid.update(scene_sha256=policy.FLAT_GROUND_SHA256,
            qualified_empty_scene=False, qualified_flat_ground_scene=True,
            actual_shape_count=1, actual_object_count=1, online_guard_limit_db=1.0,
            ground_reference_psd_evaluations=rt+friis, ground_guard_passes=friis,
            ground_guard_fallbacks=rt, ground_max_admitted_abs_error_db=.9)
        hybrid["links"][0]["last_reason"] = (
            "ground_rss_budget_exceeded" if rt else "qualified_flat_ground_online_guard")
        return row

    def test_ground_claim_requires_exact_scene_full_reference_counts_and_admitted_budget(self):
        for rt, friis in ((0, 1), (1, 0), (3, 5)):
            self.assertTrue(policy.native_dispatch_valid(self.ground_fixture(rt, friis)))
        for field, value in (
                ("scene_sha256", "b"*64), ("qualified_flat_ground_scene", False),
                ("actual_shape_count", 2), ("online_guard_limit_db", 3),
                ("ground_reference_psd_evaluations", 0), ("ground_guard_passes", 0),
                ("ground_guard_fallbacks", 2), ("ground_max_admitted_abs_error_db", 1.000001),
                ("ground_max_admitted_abs_error_db", float("nan")),
                ("ground_max_admitted_abs_error_db", True)):
            row = self.ground_fixture()
            row["hybrid_selection"][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(policy.native_dispatch_valid(row))
        row = self.ground_fixture()
        del row["hybrid_selection"]["ground_max_admitted_abs_error_db"]
        self.assertFalse(policy.native_dispatch_valid(row))

    def test_ground_fallback_count_cannot_exceed_actual_RT_outputs(self):
        row = self.ground_fixture(rt=1, friis=1)
        row["hybrid_selection"].update(ground_reference_psd_evaluations=101,
            ground_guard_passes=1, ground_guard_fallbacks=100)
        self.assertFalse(policy.native_dispatch_valid(row))

    def test_empty_v1_results_remain_readable_but_cannot_claim_ground(self):
        row = self.fixture()
        row["hybrid_selection"]["policy_id"] = policy.LEGACY_POLICY_ID
        self.assertTrue(policy.native_dispatch_valid(row))
        row = self.ground_fixture()
        row["hybrid_selection"]["policy_id"] = policy.LEGACY_POLICY_ID
        self.assertFalse(policy.native_dispatch_valid(row))

if __name__=="__main__":unittest.main()
