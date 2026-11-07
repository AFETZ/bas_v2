"""Publication cadence on controlled clocks; no Gazebo or RF acceptance claim."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from network.position_tracker import tracker


class ControlledClock:
    def __init__(self):
        self.now_ns = 1_000_000_000_000

    def monotonic_ns(self):
        return self.now_ns


class ControlledStop:
    def __init__(self, clock, interrupt_after_ns=None):
        self.clock, self.interrupt_after_ns = clock, interrupt_after_ns
        self.stopped = False
        self.waits_ns = []

    def set(self):
        self.stopped = True

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        if self.stopped:
            return True
        duration = round(seconds * 1e9)
        if duration < 0:
            raise AssertionError('negative publication wait')
        self.waits_ns.append(duration)
        if self.interrupt_after_ns is not None:
            self.clock.now_ns += min(duration, self.interrupt_after_ns)
            self.set()
        else:
            self.clock.now_ns += duration
        return self.stopped


class GazeboPublicationTests(unittest.TestCase):
    def test_serialization_time_is_inside_the_requested_publication_period(self):
        for period_ns in (20_000_000, 10_000_000):
            clock = ControlledClock()
            stop, published = ControlledStop(clock), []
            def publish():
                published.append(clock.now_ns)
                clock.now_ns += 3_000_000  # Real snapshot/serialization/write cost being diagnosed.
                if len(published) == 5:
                    stop.set()
            with self.subTest(period_ns=period_ns), patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
                tracker._publish_periodically(stop, period_ns, publish)
            self.assertEqual([value - published[0] for value in published],
                             [i * period_ns for i in range(5)])
            self.assertEqual(stop.waits_ns, [period_ns - 3_000_000] * 4)

    def test_overrun_skips_missed_ticks_without_catch_up_publications(self):
        clock = ControlledClock()
        stop, published = ControlledStop(clock), []
        def publish():
            published.append(clock.now_ns)
            clock.now_ns += 47_000_000 if len(published) == 1 else 3_000_000
            if len(published) == 4:
                stop.set()
        with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
            tracker._publish_periodically(stop, 20_000_000, publish)
        self.assertEqual([value - published[0] for value in published], [0, 60_000_000, 80_000_000, 100_000_000])
        self.assertEqual(stop.waits_ns, [13_000_000, 17_000_000, 17_000_000])

    def test_stop_interrupts_wait_and_prevents_the_next_publication(self):
        clock = ControlledClock()
        stop = ControlledStop(clock, interrupt_after_ns=5_000_000)
        published = []
        def publish():
            published.append(clock.now_ns)
            clock.now_ns += 3_000_000
        with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
            tracker._publish_periodically(stop, 20_000_000, publish)
        self.assertEqual(len(published), 1)
        self.assertEqual(clock.now_ns - published[0], 8_000_000)
        self.assertEqual(stop.waits_ns, [17_000_000])
        with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
            tracker._publish_periodically(stop, 20_000_000, publish)
        self.assertEqual(len(published), 1)

    def test_invalid_rate_is_rejected_before_native_imports_or_node_creation(self):
        for rate in (0, -1, float('nan'), float('inf'), -float('inf'), True, '50', 1e300, 1e-300):
            with self.subTest(rate=rate), self.assertRaisesRegex(ValueError, 'rate'):
                tracker.run_gazebo_tracker(SimpleNamespace(rate_hz=rate))


if __name__ == '__main__':
    unittest.main()
