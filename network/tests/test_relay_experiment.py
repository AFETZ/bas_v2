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
                                              LandingTrace, Pose, PoseReader, RegionHold, ScenarioError, main)
from scripts.product.relay_experiment import RelayExperiment, validate_experiment_profile


def pose(source, z=12., host=1000, velocity=(0., 0., 0.)):
    return Pose((1., 2., z), velocity, source, host)


def measure_clock_samples(sample_pairs, *, duration_s, task_period_s):
    """Exercise actual measure/pump/refresh with independent wall/source clocks."""
    scenario = RelayExperiment.__new__(RelayExperiment)
    scenario.args = SimpleNamespace(duration_s=duration_s, fault_start_s=1., fault_end_s=2.,
            task_period_s=task_period_s, command_period_s=1., base_altitude_m=8.,
            altitude_separation_m=4., altitude_step_m=4.)
    source, host = sample_pairs[0]
    clock = SimpleNamespace(source=source, now=host)
    samples = iter(sample_pairs)
    def step():
        clock.source, clock.now = next(samples)
    scenario.gcs = SimpleNamespace(step=step, running=True, grant=None)
    scenario.reader = SimpleNamespace(read=lambda: (clock.source,
                {i: pose(clock.source, host=clock.now) for i in range(1, 6)}))
    scenario.source_s, scenario.poses = source, {}
    scenario.observation_end_source_s = None
    scenario.last_heartbeat_ns, scenario.refresh_counter = clock.now, 0
    scenario.send = lambda *args, **fields: None  # unrelated GCS heartbeat transport
    scenario.tx = SimpleNamespace(heartbeat_encode=lambda *args: None)
    scenario.mav = SimpleNamespace(MAV_TYPE_GCS=6, MAV_AUTOPILOT_INVALID=8, MAV_STATE_ACTIVE=4)
    scenario.tails, scenario.land_commands, scenario.landing_traces = {}, {}, {}
    scenario.onboard_mode, scenario.brake_regions = {}, {}
    observed = []
    scenario.observer = SimpleNamespace(observe=lambda poses, now: observed.append(clock.source), completed={})
    scenario.summary = dict(failsafe_holds={})
    scenario.initial = {i: pose(source) for i in range(1, 6)}
    scenario.anchors = {i: dict(world_z_offset_m=0.) for i in range(1, 6)}
    events, offers = [], []
    scenario.emit = lambda event, **fields: events.append((event, fields))
    scenario.phase_name = None
    scenario.phase = lambda name: setattr(scenario, "phase_name", name)
    scenario.csv = SimpleNamespace(writerow=offers.append)
    scenario.csv_handle = SimpleNamespace(flush=lambda: None)
    with patch("scripts.product.relay_experiment.time.monotonic_ns", side_effect=lambda: clock.now):
        scenario.measure()
    return scenario, offers, events, observed


class RelayExperimentTests(unittest.TestCase):
    def test_final_pump_at_or_beyond_end_cannot_offer_refresh_goal_or_completion(self):
        for final_source in (103., 103.4):
            with self.subTest(final_source=final_source):
                samples = [(s, 10_000_000_000 + int((s - 100.) * 1e9))
                           for s in (100., 101., 102., final_source)]
                scenario, offers, events, observed = measure_clock_samples(samples, duration_s=3., task_period_s=3.)
                # Actual refresh() emitted three five-UAV batches at [0,1,2], never t>=3.
                self.assertEqual(len(offers), 15)
                self.assertTrue(all(row["source_sim_time_s"] < 103. for row in offers))
                goals = [fields for event, fields in events if event == "goal_created"]
                self.assertEqual(len(goals), 5)
                self.assertTrue(all(row["goal_id"].startswith("goal0:") for row in goals))
                self.assertEqual(observed, [100., 101., 102.])
                self.assertAlmostEqual(scenario.summary["measurement_duration_source_s"], final_source - 100.)
                self.assertEqual(scenario.summary["measurement_end_source_sim_time_s"], 103.)

    def test_wall_schedule_end_does_not_add_refresh_while_source_is_just_before_end(self):
        samples = [(100. + second, 10_000_000_000 + second * 1_000_000_000) for second in range(40)]
        samples += [(139.993, 50_008_000_000), (140.013, 50_028_000_000)]
        scenario, offers, events, observed = measure_clock_samples(samples, duration_s=40., task_period_s=20.)
        self.assertEqual(len(offers), 200)  # forty declared one-second slots for all five UAVs
        self.assertTrue(all(row["scheduled_host_monotonic_ns"] < 50_000_000_000 for row in offers))
        self.assertFalse(any(row["source_sim_time_s"] == 139.993 for row in offers))
        self.assertEqual(sum(event == "control_refresh_offered" for event, _ in events), 200)
        self.assertEqual(sum(event == "control_refresh_unavailable" for event, _ in events), 200)
        self.assertIn(139.993, observed)  # physical observation continues to declared model-time end
        self.assertNotIn(140.013, observed)
        self.assertEqual(scenario.summary["measurement_refresh_schedule_end_host_monotonic_ns"], 50_000_000_000)
        self.assertAlmostEqual(scenario.summary["measurement_duration_source_s"], 40.013)
        self.assertAlmostEqual(scenario.summary["measurement_duration_wall_s"], 40.028)

    def test_guided_is_reestablished_after_brake_and_requires_post_request_heartbeat(self):
        now = 2_000_000_000
        grant = Authority("run").issue(1, now - 100_000_000, 2_000_000_000)
        scenario = RelayExperiment.__new__(RelayExperiment)
        scenario.gcs = SimpleNamespace(grant=grant, latest_message_ns={1: {"HEARTBEAT": now - 10}})
        heartbeat = SimpleNamespace(custom_mode=17)
        scenario.fresh = lambda *args: heartbeat
        scenario.mode_epoch, scenario.mode_request_ns = {1: 1}, {1: now - 1_000_000_000}
        scenario.modes = {"GUIDED": 4}
        scenario.mav = SimpleNamespace(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED=1)
        scenario.tx = SimpleNamespace(set_mode_encode=lambda *args: args)
        sent = []
        scenario.send = lambda i, message: sent.append(message) or SimpleNamespace(created_ns=now)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=now):
            self.assertFalse(scenario.guided_ready(1))
            self.assertEqual(len(sent), 1)
            heartbeat.custom_mode = 4
            self.assertFalse(scenario.guided_ready(1))  # old cached GUIDED is insufficient
            scenario.gcs.latest_message_ns[1]["HEARTBEAT"] = now
            self.assertTrue(scenario.guided_ready(1))

    def test_new_epoch_requires_new_guided_confirmation_even_if_cached_mode_matches(self):
        issuer = Authority("run")
        old = issuer.issue(1, 3_000_000_000, 2_000_000_000)
        new = issuer.issue(2, 4_800_000_000, 2_000_000_000)
        now = new.valid_from_ns
        scenario = RelayExperiment.__new__(RelayExperiment)
        scenario.gcs = SimpleNamespace(grant=new, latest_message_ns={1: {"HEARTBEAT": now - 1}})
        scenario.fresh = lambda *args: SimpleNamespace(custom_mode=4)
        scenario.mode_epoch, scenario.mode_request_ns = {1: old.epoch}, {1: now - 100_000_000}
        scenario.modes = {"GUIDED": 4}
        scenario.mav = SimpleNamespace(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED=1)
        scenario.tx = SimpleNamespace(set_mode_encode=lambda *args: args)
        sent = []
        scenario.send = lambda i, message: sent.append(message) or SimpleNamespace(created_ns=now)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=now):
            self.assertFalse(scenario.guided_ready(1))
        self.assertEqual(scenario.mode_epoch[1], new.epoch)
        self.assertEqual(len(sent), 1)

    def test_controlled_landing_requires_uart_mode_descent_ground_then_disarm(self):
        trace = LandingTrace(pose(0., z=10., host=1000), ground_z=.5)
        trace.admit("land-command", 1100)
        trace.observe(pose(1., z=8., host=1300, velocity=(0., 0., -1.)),
                      heartbeat_ns=1200, mode_land=True, armed=True)
        self.assertEqual(trace.events, ["land_mode_observed", "landing_descent_observed"])
        trace.observe(pose(2., z=.5, host=2000), heartbeat_ns=1900, mode_land=True, armed=True)
        self.assertFalse(trace.completed)
        trace.observe(pose(3., z=.5, host=2300), heartbeat_ns=2200, mode_land=True, armed=False)
        self.assertTrue(trace.completed)
        self.assertIsNone(trace.terminal_reason)

    def test_post_takeoff_ground_disarm_without_our_land_is_terminal_failure(self):
        trace = LandingTrace(pose(0., z=.5, host=1000), ground_z=.5)
        trace.observe(pose(1., z=.5, host=2000), heartbeat_ns=2000, mode_land=False, armed=False)
        self.assertFalse(trace.completed)
        self.assertIn("terminal_disarm", trace.terminal_reason)

    def test_old_land_mode_before_our_uart_admission_cannot_prove_landing(self):
        trace = LandingTrace(pose(0., z=10., host=1000), ground_z=.5)
        trace.admit("new-land-command", 2000)
        trace.observe(pose(1., z=8., host=2100, velocity=(0., 0., -1.)),
                      heartbeat_ns=1900, mode_land=True, armed=True)
        self.assertIsNone(trace.mode_ns)
        trace.observe(pose(2., z=.5, host=2200), heartbeat_ns=1900, mode_land=True, armed=False)
        self.assertFalse(trace.completed)
        self.assertIsNotNone(trace.terminal_reason)

    def test_land_mode_plus_uncontrolled_fast_fall_is_not_controlled_land(self):
        trace = LandingTrace(pose(0., z=10., host=1000), ground_z=.5, max_descent_mps=3.)
        trace.admit("land-command", 1100)
        trace.observe(pose(1., z=7., host=1300, velocity=(0., 0., -8.)),
                      heartbeat_ns=1200, mode_land=True, armed=True)
        trace.observe(pose(2., z=.5, host=2000), heartbeat_ns=1900, mode_land=True, armed=True)
        trace.observe(pose(3., z=.5, host=2300), heartbeat_ns=2200, mode_land=True, armed=False)
        self.assertFalse(trace.completed)
        self.assertIsNotNone(trace.terminal_reason)

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

    def test_sensitivity_accepts_only_the_declared_separate_design(self):
        args = SimpleNamespace(experiment_profile="sensitivity", pilot=False,
            fault_profile="selective", duration_s=180, fault_start_s=47.123, fault_end_s=47.623)
        for duration in (.5, 2., 10.):
            args.fault_end_s = args.fault_start_s + duration
            validate_experiment_profile(args)
        for change in (dict(pilot=True), dict(fault_profile="none"), dict(duration_s=90),
                       dict(fault_start_s=39), dict(fault_end_s=50.123)):
            invalid = SimpleNamespace(**(vars(args) | change))
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_experiment_profile(invalid)


if __name__ == "__main__":
    unittest.main()
