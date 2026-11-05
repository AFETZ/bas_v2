"""Focused adapter/control-input fixtures; no simulated flight results here."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from network.coordination.policy import Authority, make_command
from scripts.product.relay_experiment import Goal, GoalObserver, Pose, RelayExperiment, ScenarioError, validate_experiment_profile
from scripts.product.urban_mission import NedFrame, UrbanScenario

NS = 1_000_000_000


def scenario_dict():
    return dict(spawns={str(i): [10*i, 0, 2] for i in range(1, 6)},
                routes={str(i): [[10*i+10, 0, 12], [10*i+10, 10, 12]] for i in range(1, 6)},
                mission=dict(world_ground_reference_z_m=.195, telemetry_max_age_s=.5,
                             speed_mps=3, waypoint_dwell_s=2))


def load(raw):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scenario.json"
        path.write_text(json.dumps(raw))
        return UrbanScenario.load(path)


def control_fixture():
    scenario = RelayExperiment.__new__(RelayExperiment)
    scenario.urban = load(scenario_dict())
    scenario.urban_frames = {i: scenario.urban.frame(i, (0, 0, 0)) for i in range(1, 6)}
    scenario.urban_holds, scenario.urban_last_sample = {}, {}
    scenario.urban_barrier_state = {}
    scenario.urban_indices = {i: 0 for i in range(1, 6)}
    scenario.urban_reached = {i: [] for i in range(1, 6)}
    scenario.args = SimpleNamespace(hold_s=2., z_tolerance_m=1.5, xy_tolerance_m=3, hold_speed_mps=1)
    scenario.source_s = 500
    scenario.gcs = SimpleNamespace(latest_messages={i: {} for i in range(1, 6)},
        latest_message_ns={i: {} for i in range(1, 6)},
        latest_message_received_ns={i: {} for i in range(1, 6)})
    scenario.poses = {i: Pose((1e6, 1e6, 12), (0, 0, 0), 500, NS) for i in range(1, 6)}
    events = []
    scenario.emit = lambda event, **fields: events.append((event, fields))
    return scenario, events


def radio_sample(scenario, producer_ns, boot_s, world_points=None):
    for i in range(1, 6):
        world = world_points[i] if world_points else scenario.urban.routes[i][0]
        n, e, d = scenario.urban_frames[i].to_ned(world)
        scenario.gcs.latest_messages[i]["LOCAL_POSITION_NED"] = SimpleNamespace(
            x=n, y=e, z=d, vx=0, vy=0, vz=0, time_boot_ms=int(round(boot_s*1000)))
        scenario.gcs.latest_message_ns[i]["LOCAL_POSITION_NED"] = producer_ns
        scenario.gcs.latest_message_received_ns[i]["LOCAL_POSITION_NED"] = producer_ns


class UrbanMissionTests(unittest.TestCase):
    def test_route_dwell_three_does_not_overwrite_physical_hold_two_or_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = scenario_dict()
            raw["mission"]["waypoint_dwell_s"] = 3
            config = root / "scene.json"
            config.write_text(json.dumps(raw))
            args = SimpleNamespace(run_dir=root / "run", phase_file=root / "phase", run_id="unit",
                node_state=root / "node_state.json",
                urban_scenario=config, policy="fixed", initial_master=1, radio_watchdog_file=None,
                watchdog_s=.3, hold_s=2., z_tolerance_m=1.5, xy_tolerance_m=3, hold_speed_mps=1,
                fault_profile="none", pilot=True, experiment_profile="urban", command_period_s=1,
                task_period_s=20)
            fake_gcs = SimpleNamespace(mavutil=SimpleNamespace(mavlink=SimpleNamespace(
                MAVLink=lambda *a, **kw: None), mode_mapping_acm={4: "GUIDED"}))
            (args.run_dir / "logs").mkdir(parents=True)
            with patch("scripts.product.relay_experiment.GCSService", return_value=fake_gcs), \
                    patch("scripts.product.relay_experiment.Events", return_value=lambda *a, **kw: None):
                scenario = RelayExperiment(args)
            try:
                self.assertEqual(scenario.observer.args.hold_s, 2)
                scenario.source_s = 1
                scenario.urban_goal(1, 0)
                self.assertEqual(scenario.urban_holds[1].hold_s, 3)
                exported = json.loads((args.run_dir / "metrics/relay_experiment_config.json").read_text())
                self.assertEqual(exported["hold_s"], 2)
                self.assertEqual(exported["urban_completion_criteria"], {
                    "route_waypoint_dwell_s": 3,
                    "route_dwell_clock": "autopilot_time_boot_ms",
                    "physical_completion_hold_s": 2,
                    "physical_completion_dwell_clock": "source_sim_time"})
                self.assertEqual(scenario.summary["physical_completion_hold_s"], 2)
                self.assertEqual(scenario.summary["route_waypoint_dwell_s"], 3)
            finally:
                scenario.csv_handle.close()

    def test_barrier_waits_for_reached_waypoint_and_fresh_radio_then_releases_once(self):
        scenario, events = control_fixture()
        raw = scenario_dict()
        raw["mission"]["barriers"] = {"1:1": {"minimum_reached": {"2": 0}}}
        scenario.urban = load(raw)
        goals = {}
        # UAV1 reaches the corner, while UAV2 has not reached the required point.
        for boot in (0., .5, 1., 1.5, 2., 2.5):
            now = NS + int(boot*NS)
            points = {i: scenario.urban.routes[i][0] for i in range(1, 6)}
            points[2] = (120., 0., 12.)
            radio_sample(scenario, now, boot, points)
            with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=now):
                scenario.advance_urban(goals)
        self.assertEqual(scenario.urban_indices[1], 0)
        self.assertEqual(scenario.urban_reached[1], [0])  # current point reached; next not released
        self.assertEqual(sum(event == "barrier_wait" for event, _ in events), 1)
        # Even an earlier valid reached record is insufficient with stale telemetry.
        scenario.urban_reached[2] = [0]
        for boot in (3., 3.5, 4.):
            now = NS + int(boot*NS)
            radio_sample(scenario, now, boot)
            scenario.gcs.latest_message_ns[2]["LOCAL_POSITION_NED"] = 3*NS
            scenario.gcs.latest_message_received_ns[2]["LOCAL_POSITION_NED"] = 3*NS
            with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=now):
                scenario.advance_urban(goals)
        self.assertEqual(scenario.urban_indices[1], 0)
        radio_sample(scenario, 5_500_000_000, 4.5)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=5_500_000_000):
            scenario.advance_urban(goals)
            scenario.advance_urban(goals)
        self.assertEqual(scenario.urban_indices[1], 1)
        self.assertEqual(scenario.urban_reached[1], [0])
        releases = [fields for event, fields in events if event == "barrier_released"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["reference_telemetry_producer_ns"], {"2": 5_500_000_000})

    def test_barrier_rejects_unknown_or_impossible_references(self):
        for key, required in (("1:5", {"2": 0}), ("1:1", {"2": 4}),
                              ("1:1", {"6": 0}), ("1:1", {"1": 1})):
            raw = scenario_dict()
            raw["mission"]["barriers"] = {key: {"minimum_reached": required}}
            with self.assertRaises(ValueError):
                load(raw)

    def test_local_frame_has_correct_axes_sign_and_nonzero_origin(self):
        frame = NedFrame((20., 30., .195), (2., -1., .25))
        self.assertEqual(frame.to_ned((25., 33., 12.195)), (5., 4., -11.75))
        self.assertEqual(frame.to_world((5., 4., -11.75)), (25., 33., 12.195))
        self.assertEqual(frame.velocity_to_world((1., 2., -3.)), (2., 1., 3.))

    def test_invalid_routes_and_undeclared_ground_reference_fail(self):
        for mutate in (lambda r: r["routes"].pop("5"),
                       lambda r: r["routes"]["1"].clear(),
                       lambda r: r["routes"]["1"].append([float("nan"), 1, 12]),
                       lambda r: r["mission"].update(ned_axes="guess_from_heading"),
                       lambda r: r["mission"].update(telemetry_max_age_s=1)):
            raw = scenario_dict()
            mutate(raw)
            with self.assertRaises(ValueError):
                load(raw)

    def test_ground_truth_cannot_progress_a_route_without_radio_telemetry(self):
        scenario, events = control_fixture()
        goals = {}
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=NS):
            scenario.advance_urban(goals)
            # Arbitrarily long source-time progress and exactly reached truth poses
            # still cannot substitute for any radio-delivered control sample.
            scenario.source_s += 200
            scenario.poses = {i: Pose(scenario.urban.routes[i][0], (0, 0, 0), 700, NS)
                              for i in range(1, 6)}
            scenario.advance_urban(goals)
        self.assertEqual(list(scenario.urban_indices.values()), [0]*5)
        self.assertFalse(any(event == "waypoint_reached_telemetry" for event, _ in events))

    def test_sequential_progression_needs_new_samples_and_never_skips_a_corner(self):
        scenario, events = control_fixture()
        goals = {}
        for boot in (0., .5, 1., 1.5, 2.):
            now = NS + int(boot*NS)
            radio_sample(scenario, now, boot)
            with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=now):
                scenario.advance_urban(goals)
                scenario.advance_urban(goals)  # repeated publication cannot advance dwell
        self.assertEqual(list(scenario.urban_indices.values()), [1]*5)
        self.assertTrue(all(goal.goal_id.startswith("waypoint1:") for goal in goals.values()))
        self.assertEqual(sum(event == "waypoint_reached_telemetry" for event, _ in events), 5)
        # Route 1 remains pending even though source time grows far beyond old task cadence.
        scenario.source_s += 100
        radio_sample(scenario, 4*NS, 3.)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=4*NS):
            scenario.advance_urban(goals)
        self.assertEqual(list(scenario.urban_indices.values()), [1]*5)
        self.assertTrue(all(reached == [0] for reached in scenario.urban_reached.values()))

    def test_stale_radio_pose_cannot_complete_dwell(self):
        scenario, events = control_fixture()
        goals = {}
        for boot in (0., .5, 1., 1.5):
            now = NS + int(boot*NS)
            radio_sample(scenario, now, boot)
            with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=now):
                scenario.advance_urban(goals)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=4*NS):
            scenario.advance_urban(goals)
        self.assertTrue(all(hold.started_s is None for hold in scenario.urban_holds.values()))
        self.assertFalse(any(event == "waypoint_reached_telemetry" for event, _ in events))

    def test_autopilot_time_reset_fails_instead_of_reanchoring(self):
        scenario, _ = control_fixture()
        goals = {}
        radio_sample(scenario, NS, 20.)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=NS):
            scenario.advance_urban(goals)
        radio_sample(scenario, 2*NS, 1.)
        with patch("scripts.product.relay_experiment.time.monotonic_ns", return_value=2*NS):
            with self.assertRaisesRegex(ScenarioError, "clock reset"):
                scenario.advance_urban(goals)

    def test_urban_setpoint_is_native_local_ned_with_frame_translation(self):
        scenario, _ = control_fixture()
        scenario.tx = SimpleNamespace(set_position_target_local_ned_encode=lambda *values: values)
        scenario.mav = SimpleNamespace(MAV_FRAME_LOCAL_NED=1)
        goal = Goal("waypoint", 1, NS, 0, 12, (25., 13., 12.195))
        with patch("scripts.product.relay_experiment.time.monotonic", return_value=1):
            message = scenario.setpoint(1, goal)
        self.assertEqual(message[:8], (1000, 1, 1, 1, 3576, 13., 15., -12.))

    def test_horizontal_completion_records_real_xy_motion_with_zero_height_change(self):
        events = []
        args = SimpleNamespace(hold_s=2, z_tolerance_m=1.5, xy_tolerance_m=3, hold_speed_mps=1)
        observer = GoalObserver(lambda event, **fields: events.append((event, fields)), args)
        grant = Authority("run").issue(1, NS, 10*NS)
        command = make_command(grant, destination_id=1, sequence=1, now_ns=NS, payload=b"fixture")
        goal = Goal("waypoint0:uav1", 1, NS, 0., 12., (6., 0., 12.),
                    waypoint_index=0, waypoint_id="waypoint0:uav1", maneuver_kind="horizontal")
        observer.register(command, goal)
        observer.admission(dict(event="uart_admitted", command_id=command.command_id,
                                epoch=1, uav_id=1, host_monotonic_ns=NS))
        observer.observe({1: Pose((0., 0., 12.), (0, 0, 0), 0., NS)}, NS)
        for boot in (.5, 1., 1.5, 2., 2.5):
            observer.observe({1: Pose(goal.target, (0, 0, 0), boot, NS+int(boot*NS))}, NS+int(boot*NS))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["max_horizontal_displacement_m"], 6)
        self.assertEqual(events[0][1]["max_vertical_displacement_m"], 0)
        self.assertTrue(events[0][1]["maneuver_observed"])

    def test_urban_profile_disallows_scheduled_fault_and_preserves_other_profiles(self):
        args = SimpleNamespace(experiment_profile="urban", urban_scenario=Path("scene.json"),
                               fault_profile="none", pilot=False)
        validate_experiment_profile(args)
        args.fault_profile = "selective"
        with self.assertRaises(ValueError):
            validate_experiment_profile(args)


if __name__ == "__main__":
    unittest.main()
