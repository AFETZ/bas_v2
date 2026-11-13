"""Production clock/odometry interleavings; host and source clocks are independent."""
from types import SimpleNamespace as NS
import math
import unittest

from network.position_tracker.tracker import LiveState


def odometry(source_ns):
    vector = lambda: NS(x=0., y=0., z=0.)
    sec, nanosec = divmod(source_ns, 1_000_000_000)
    return NS(header=NS(stamp=NS(sec=sec, nanosec=nanosec), frame_id='odom'),
              child_frame_id='base_link',
              pose=NS(pose=NS(position=vector(), orientation=NS(x=0., y=0., z=0., w=1.))),
              twist=NS(twist=NS(linear=vector(), angular=vector())))


class MeasuredClockTests(unittest.TestCase):
    def setUp(self):
        self.state = LiveState(dict(command_post=dict(position_m=[0, 0, 1]),
                                    robots=[dict(name='uav1'), dict(name='uav2')]), .1)

    def receive(self, source_ns, host_ns, name='uav1'):
        return self.state.odometry(name, odometry(source_ns), host_ns)

    def latest(self, name='uav1'):
        return self.state.history[name][-1]

    def test_delayed_delivery_preserves_age_and_does_not_roll_back_next_sample(self):
        self.state.clock(10., 1_000_000_000)
        self.assertTrue(self.receive(10_000_000_000, 1_090_000_000))
        self.assertEqual(self.latest()['sample_monotonic_ns'], 1_000_000_000)
        self.assertEqual(self.latest()['received_monotonic_ns'], 1_090_000_000)
        self.assertEqual(self.state.snapshot(1_090_000_000)['nodes'][1]['sample_age_ms'], 90.)
        self.state.clock(10.1, 1_100_000_000)
        self.assertTrue(self.receive(10_020_000_000, 1_101_000_000))
        self.assertEqual(self.latest()['sample_monotonic_ns'], 1_020_000_000)
        self.assertEqual(self.state.snapshot(1_101_000_000)['nodes'][1]['sample_age_ms'], 81.)

    def test_same_source_epoch_has_same_host_epoch_across_different_arrival_times(self):
        self.state.clock(10., 1_000_000_000)
        self.assertTrue(self.receive(9_980_000_000, 1_010_000_000, 'uav1'))
        self.assertTrue(self.receive(9_980_000_000, 1_060_000_000, 'uav2'))
        self.assertEqual(self.latest('uav1')['sample_monotonic_ns'], 980_000_000)
        self.assertEqual(self.latest('uav1')['sample_monotonic_ns'],
                         self.latest('uav2')['sample_monotonic_ns'])

    def test_clock_lag_and_delivery_delay_share_one_age_budget(self):
        self.state.clock(10., 1_000_000_000)
        self.assertFalse(self.receive(9_940_000_000, 1_060_000_000))
        self.assertEqual(self.state.rejection_reasons['sample_stale'], 1)
        self.assertFalse(self.state.history['uav1'])

    def test_clock_delivery_jitter_rejects_host_rollback_without_retiming(self):
        self.state.clock(10., 1_000_000_000)
        self.assertTrue(self.receive(10_000_000_000, 1_000_000_000))
        previous = dict(self.latest())
        self.state.clock(10.1, 1_010_000_000)
        self.assertFalse(self.receive(10_020_000_000, 1_011_000_000))
        self.assertEqual(self.state.rejection_reasons['sample_time_regression'], 1)
        self.assertEqual(self.latest(), previous)
        self.assertTrue(self.state.snapshot(1_101_000_000)['nodes'][1]['stale'])

    def test_repeated_clock_does_not_refresh_measurements(self):
        self.state.clock(10., 1_000_000_000)
        self.state.clock(10., 1_090_000_000)
        self.assertTrue(self.receive(10_000_000_000, 1_090_000_000))
        self.assertEqual(self.latest()['sample_monotonic_ns'], 1_000_000_000)
        self.assertFalse(self.receive(10_010_000_000, 1_101_000_000))
        self.assertEqual(self.state.rejection_reasons['clock_stale'], 1)

    def test_odometry_ahead_of_last_clock_uses_conservative_observed_anchor(self):
        self.state.clock(10., 1_000_000_000)
        self.assertTrue(self.receive(10_020_000_000, 1_010_000_000))
        self.assertEqual(self.latest()['sample_monotonic_ns'], 1_000_000_000)
        self.assertLessEqual(self.latest()['sample_monotonic_ns'], self.latest()['received_monotonic_ns'])

    def test_source_reset_and_out_of_order_checks_remain_effective(self):
        self.state.clock(10., 1_000_000_000)
        self.assertTrue(self.receive(10_000_000_000, 1_000_000_000))
        self.assertFalse(self.receive(10_000_000_000, 1_001_000_000))
        self.assertEqual(self.state.rejection_reasons['out_of_order'], 1)
        self.state.clock(9., 1_002_000_000)
        self.assertEqual(self.state.snapshot(1_003_000_000)['fault'], 'gazebo_clock_reset_restart_required')

    def test_lockstep_freshness_uses_source_time_not_host_waits(self):
        self.state.simulation_mode = 'lockstep'
        self.state.clock(10., 1_000_000_000)
        self.assertTrue(self.receive(9_980_000_000, 5_000_000_000))
        node = self.state.snapshot(8_000_000_000)['nodes'][1]
        self.assertAlmostEqual(node['sample_age_ms'], 20.)
        self.assertFalse(node['stale'])

    def test_measured_motion_preserves_epochs_and_world_velocity_at_delayed_delivery(self):
        # Analytic yaw cases exercise child-frame -> ENU motion independently
        # of the live timestamp mapping and age-based publication pruning.
        for speed in (0., 3., 30.):
            for yaw in (0., math.pi/2, math.pi, -math.pi/2):
                for delay_ns in (0, 25_000_000, 99_000_000, 100_000_001):
                    with self.subTest(speed=speed, yaw=yaw, delay_ns=delay_ns):
                        state = LiveState(dict(command_post=dict(position_m=[0, 0, 1]),
                                               robots=[dict(name='uav1')]), .1)
                        state.clock(10., 1_000_000_000)
                        message = odometry(10_000_000_000)
                        message.pose.pose.position = NS(x=17., y=-9., z=40.)
                        message.pose.pose.orientation = NS(x=0., y=0., z=math.sin(yaw/2), w=math.cos(yaw/2))
                        message.twist.twist.linear = NS(x=speed, y=0., z=-2.)
                        message.twist.twist.angular = NS(x=0., y=0., z=.3)
                        accepted = state.odometry('uav1', message, 1_000_000_000+delay_ns)
                        self.assertEqual(accepted, delay_ns <= 100_000_000)
                        if not accepted:
                            self.assertEqual(state.rejection_reasons['clock_stale'], 1)
                            self.assertFalse(state.history['uav1'])
                            continue
                        sample = state.history['uav1'][-1]
                        self.assertEqual(sample['sample_monotonic_ns'], 1_000_000_000)
                        self.assertEqual(sample['source_sim_time_s'], 10.)
                        self.assertEqual(sample['position_m'], [17., -9., 40.])
                        for observed, expected in zip(sample['velocity_enu_mps'],
                                                     [speed*math.cos(yaw), speed*math.sin(yaw), -2.]):
                            self.assertAlmostEqual(observed, expected, places=12)
                        self.assertEqual(sample['angular_velocity_enu_radps'], [0., 0., .3])
                        view = state.snapshot(1_100_000_000)['nodes'][1]
                        self.assertEqual(view['sample_age_ms'], 100.)
                        self.assertFalse(view['stale'])
                        self.assertEqual(view['history'], [sample])
                        expired = state.snapshot(1_100_000_001)['nodes'][1]
                        self.assertTrue(expired['stale'])
                        self.assertEqual(expired['history'], [])
                        self.assertEqual(state.history['uav1'][-1], sample)

    def fill_history(self):
        for index in range(32):
            source_ns = 10_000_000_000 + index*20_000_000
            host_ns = 1_000_000_000 + index*20_000_000
            self.state.clock(source_ns/1e9, host_ns)
            self.assertTrue(self.receive(source_ns, host_ns))
        return host_ns

    def test_live_history_keeps_every_timely_sample_including_exact_age_boundary(self):
        now = self.fill_history()
        node = self.state.snapshot(now)['nodes'][1]
        self.assertEqual([sample['sample_monotonic_ns'] for sample in node['history']],
                         [now-offset for offset in range(100_000_000, -1, -20_000_000)])
        self.assertEqual(len(self.state.history['uav1']), 32)
        self.assertEqual(node['sample_monotonic_ns'], now)
        self.assertEqual(node['sample_age_ms'], 0.)
        self.assertFalse(node['stale'])

    def test_expired_publication_stays_stale_and_keeps_original_archive_sample(self):
        now = self.fill_history()
        node = self.state.snapshot(now+100_000_001)['nodes'][1]
        self.assertEqual(node['history'], [])
        self.assertTrue(node['stale'])
        self.assertEqual(node['sample_monotonic_ns'], now)
        self.assertEqual(len(self.state.history['uav1']), 32)

    def test_lockstep_history_is_not_pruned_by_host_waits(self):
        self.state.simulation_mode = 'lockstep'
        now = self.fill_history()
        node = self.state.snapshot(now+5_000_000_000)['nodes'][1]
        self.assertEqual(len(node['history']), 32)
        self.assertEqual(node['sample_age_ms'], 0.)


if __name__ == '__main__':
    unittest.main()
