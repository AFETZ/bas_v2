"""Compact historical probe bodies exercise real selection and timeout behavior."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from network.coordination.policy import Authority
from network.coordination.service import GCSService
from scripts.product.relay_experiment import RelayExperiment, ScenarioError

# Exact first UAV1 echo inputs from each retained run, not their full journals.
FAILED = dict(now=722241666352410, echo_ns=722241656833685,
              sent_ns=722241657542665,
              peer_echo_ns={"5": 722241637459292, "4": 722241640005509,
                            "2": 722241435777485})
SUCCESS = dict(now=715638815738217, echo_ns=715638808803584,
               sent_ns=715638810275748,
               peer_echo_ns={"3": 715638699507624, "4": 715638700316347,
                             "2": 715638705439241, "5": 715638712064485})


def service(fixture):
    gcs = GCSService.__new__(GCSService)
    gcs.uav_ids = tuple(range(1, 6))
    gcs.args = SimpleNamespace(detection_s=1, initial_master=1, policy="failover",
                              renew_s=.5, lease_s=2)
    body = {key: copy.deepcopy(value) for key, value in fixture.items() if key != "now"}
    gcs.heartbeat = {1: (fixture["now"], body)}
    gcs.peer_rtt_ns = {1: fixture["now"] - fixture["echo_ns"]}
    gcs.authority = Authority("selection-fixture")
    gcs.last_renew_ns = 0
    gcs.detected_unavailable_leader = None
    gcs.grants_by_epoch, gcs.issued_grants = {}, {}
    gcs.emit, gcs.send = Mock(), Mock()
    return gcs


class SelectionDiagnosticsTests(unittest.TestCase):
    def test_historical_missing_peer_is_explained_without_issuing_a_grant(self):
        gcs = service(FAILED)
        before = copy.deepcopy((gcs.heartbeat, gcs.authority.__dict__))
        diagnostic = gcs.selection_diagnostics(FAILED["now"])
        self.assertEqual(diagnostic["available_candidates"], [])
        self.assertEqual(diagnostic["peer_reports"]["1"], dict(
            direct_available=True, missing_or_invalid_peer_ids=[3],
            stale_or_future_peer_ids=[]))
        self.assertEqual((gcs.heartbeat, gcs.authority.__dict__), before)
        gcs.emit.assert_not_called()
        gcs.send.assert_not_called()
        gcs.update_role(FAILED["now"])
        gcs.send.assert_not_called()
        self.assertIsNone(gcs.authority.leader_id)

    def test_canonical_first_echo_still_allows_the_existing_first_grant(self):
        gcs = service(SUCCESS)
        diagnostic = gcs.selection_diagnostics(SUCCESS["now"])
        self.assertEqual(diagnostic["available_candidates"], [1])
        self.assertEqual(diagnostic["peer_reports"]["1"], dict(
            direct_available=True, missing_or_invalid_peer_ids=[],
            stale_or_future_peer_ids=[]))
        gcs.update_role(SUCCESS["now"])
        self.assertEqual(gcs.authority.leader_id, 1)
        self.assertEqual(gcs.send.call_count, 5)
        self.assertEqual(gcs.emit.call_args.args, ("grant_issued",))

    def test_stale_future_and_invalid_peer_inputs_are_separate_from_direct_loss(self):
        fixture = copy.deepcopy(SUCCESS)
        fixture["peer_echo_ns"].update({"2": fixture["now"] - 1_000_000_001,
                                       "3": fixture["now"] + 1, "4": True})
        gcs = service(fixture)
        diagnostic = gcs.selection_diagnostics(fixture["now"])
        self.assertEqual(diagnostic["available_candidates"], [])
        self.assertEqual(diagnostic["peer_reports"]["1"], dict(
            direct_available=True, missing_or_invalid_peer_ids=[4],
            stale_or_future_peer_ids=[2, 3]))
        missing = diagnostic["peer_reports"]["2"]
        self.assertFalse(missing["direct_available"])
        self.assertEqual(missing["missing_or_invalid_peer_ids"], [1, 3, 4, 5])

    def test_existing_timeout_keeps_its_exception_and_first_failure_snapshot(self):
        mission = RelayExperiment.__new__(RelayExperiment)
        mission.gcs = service(FAILED)
        mission.summary, mission.emit, mission.pump = {}, Mock(), Mock()
        with patch("network.coordination.service.time.monotonic_ns", return_value=FAILED["now"]):
            with self.assertRaisesRegex(ScenarioError, "^timeout: five real heartbeats and GPS fixes$"):
                mission.wait(lambda: False, 0, "five real heartbeats and GPS fixes")
        first = copy.deepcopy(mission.summary["selection_diagnostics"])
        self.assertEqual(first["peer_reports"]["1"]["missing_or_invalid_peer_ids"], [3])
        self.assertEqual(mission.emit.call_args.args, ("mission_wait_timeout",))
        mission.gcs = service(SUCCESS)
        with patch("network.coordination.service.time.monotonic_ns", return_value=SUCCESS["now"]):
            with self.assertRaisesRegex(ScenarioError, "^timeout: cleanup wait$"):
                mission.wait(lambda: False, 0, "cleanup wait")
        self.assertEqual(mission.summary["selection_diagnostics"], first)

    def test_successful_wait_does_not_snapshot_or_emit_a_failure(self):
        mission = RelayExperiment.__new__(RelayExperiment)
        mission.gcs = service(SUCCESS)
        mission.summary, mission.emit, mission.pump = {}, Mock(), Mock()
        mission.wait(lambda: True, 1, "five real heartbeats and GPS fixes")
        self.assertEqual(mission.summary, {})
        mission.emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
