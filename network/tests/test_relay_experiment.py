"""Observation/cadence fixtures, never a substitute for the real flight run."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from network.coordination.policy import Authority, make_command
from scripts.product.relay_experiment import (EventTail, Goal, GoalObserver,
                                              Pose, PoseReader, RegionHold, ScenarioError, main)


def pose(source, z=12., host=1000, velocity=(0., 0., 0.)):
    return Pose((1., 2., z), velocity, source, host)


class RelayExperimentTests(unittest.TestCase):
    def test_repeated_pose_does_not_advance_hold(self):
        held = RegionHold((1., 2., 12.), hold_s=2)
        self.assertFalse(held.observe(pose(5)))
        for _ in range(100):
            self.assertFalse(held.observe(pose(5)))

    def test_gap_or_movement_breaks_continuous_hold(self):
        held = RegionHold((1., 2., 12.), hold_s=2)
        for source in (0., .4, .8, 1.2):
            self.assertFalse(held.observe(pose(source)))
        self.assertFalse(held.observe(pose(2.)))  # missing >0.5 s resets dwell
        self.assertFalse(held.observe(pose(2.4, velocity=(2., 0., 0.))))
        for source in (2.8, 3.2, 3.6, 4., 4.4):
            self.assertFalse(held.observe(pose(source)))
        self.assertTrue(held.observe(pose(4.8)))

    def test_uart_admission_required_and_refresh_keeps_same_goal_dwell(self):
        events = []
        args = SimpleNamespace(hold_s=2, z_tolerance_m=1.5, xy_tolerance_m=3, hold_speed_mps=1)
        observer = GoalObserver(lambda event, **kw: events.append((event, kw)), args)
        grant = Authority("run").issue(1, 1000, 100_000)
        goal = Goal("goal0:uav1", 1, 1000, 0., 12., (1., 2., 12.))
        one = make_command(grant, destination_id=1, sequence=1, now_ns=1000, payload=b"unit-frame")
        observer.register(one, goal)
        for s in (0., .5, 1., 1.5, 2.):
            observer.observe({1: pose(s)}, now_ns=1001)
        self.assertEqual(events, [])
        observer.admission(dict(event="uart_admitted", command_id=one.command_id,
                                epoch=1, uav_id=1, host_monotonic_ns=1000))
        for s in (3., 3.5, 4.):
            observer.observe({1: pose(s)}, now_ns=1001)
        refresh = dataclasses.replace(one, sequence=2)
        observer.register(refresh, goal)
        observer.admission(dict(event="uart_admitted", command_id=refresh.command_id,
                                epoch=1, uav_id=1, host_monotonic_ns=1001))
        for s in (4.5, 5.):
            observer.observe({1: pose(s, host=1002)}, now_ns=1002)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["goal_id"], goal.goal_id)
        self.assertEqual(events[0][1]["command"].command_id, refresh.command_id)
        self.assertFalse(events[0][1]["maneuver_observed"])
        # Repeating the old physical goal under new refresh IDs is not another action.
        for s in (5.5, 6., 6.5, 7.):
            observer.observe({1: pose(s, host=1002)}, now_ns=1002)
        self.assertEqual(len(events), 1)

    def test_pose_before_admission_or_after_lease_expiry_cannot_complete(self):
        events = []
        args = SimpleNamespace(hold_s=2, z_tolerance_m=1.5, xy_tolerance_m=3, hold_speed_mps=1)
        observer = GoalObserver(lambda *a, **k: events.append(a), args)
        grant = Authority("run").issue(1, 1000, 100)
        command = make_command(grant, destination_id=1, sequence=1, now_ns=1000, payload=b"unit-frame")
        goal = Goal("goal0:uav1", 1, 1000, 0., 12., (1., 2., 12.))
        observer.register(command, goal)
        observer.admission(dict(event="uart_admitted", command_id=command.command_id,
                                epoch=1, uav_id=1, host_monotonic_ns=1050))
        for s in (0., .5, 1., 1.5, 2.):
            observer.observe({1: pose(s, host=1000)}, now_ns=1060)
            observer.observe({1: pose(s, host=1200)}, now_ns=1200)
        self.assertEqual(events, [])

    def test_old_goal_identity_is_retained_after_coordinator_switch(self):
        events = []
        args = SimpleNamespace(hold_s=.5, z_tolerance_m=1.5, xy_tolerance_m=3, hold_speed_mps=1)
        observer = GoalObserver(lambda event, **kw: events.append(kw), args)
        issuer = Authority("run")
        issuer.issue(1, 1000, 100)
        new = issuer.issue(2, 1001, 1000)
        command = make_command(new, destination_id=1, sequence=1, now_ns=1100, payload=b"unit-frame")
        old_goal = Goal("old-goal", 1, 1000, 1., 12., (1., 2., 12.))
        observer.register(command, old_goal)
        observer.admission(dict(event="uart_admitted", command_id=command.command_id,
                                epoch=2, uav_id=1, host_monotonic_ns=1100))
        observer.observe({1: pose(5., host=1200)}, 1200)
        observer.observe({1: pose(5.5, host=1200)}, 1200)
        self.assertEqual(events[0]["goal_created_host_monotonic_ns"], 1000)
        self.assertEqual(events[0]["goal_id"], "old-goal")
        self.assertEqual(events[0]["command"].epoch, 2)

    def test_incomplete_event_line_is_read_when_completed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text('{"event":"one"}\n{"event":')
            tail = EventTail(path)
            self.assertEqual(tail.read(), [{"event": "one"}])
            with path.open("a") as stream:
                stream.write('"two"}\n')
            self.assertEqual(tail.read(), [{"event": "two"}])

    def test_native_pose_age_and_causal_history_are_enforced(self):
        now = time.monotonic_ns()
        rows = []
        for i in range(1, 6):
            causal = dict(position_m=[i, 0., 0.], velocity_enu_mps=[0., 0., 0.],
                          source_sim_time_s=10., sample_monotonic_ns=now - 10_000_000)
            future = dict(causal, source_sim_time_s=10.01)
            rows.append(dict(future, id=f"uav{i}", history=[causal, future]))
        state = dict(schema_version=2, coordinate_frame="ENU", source="gazebo_odometry",
                     session_id="same", fault=None, published_monotonic_ns=now,
                     clock_received_monotonic_ns=now, source_sim_time_s=10., nodes=rows)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "node_state.json"
            path.write_text(json.dumps(state))
            reader = PoseReader(path)
            _, poses = reader.read()
            self.assertTrue(all(p.source_s == 10. for p in poses.values()))
            state["published_monotonic_ns"] = now - 600_000_000
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ScenarioError, "stale"):
                reader.read()

    def test_nonstandard_main_windows_require_explicit_pilot(self):
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                main(["--run-dir", "/tmp/unused", "--node-state", "/tmp/unused.json",
                      "--phase-file", "/tmp/unused.phase", "--policy", "fixed",
                      "--duration-s", "30", "--fault-start-s", "10", "--fault-end-s", "20"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
