"""Deterministic virtual-UART byte clocks; these are not physical baud measurements."""
import unittest

from network.scripts.serial_transport import SerialBaudBudget


class SerialBaudBudgetTests(unittest.TestCase):
    def test_complete_batch_boundaries_carry_fractional_bit_time_without_drift(self):
        budget = SerialBaudBudget(115200, 10)
        budget.start(0)
        for total, due in ((192,16_666_667),(384,33_333_334),(576,50_000_000)):
            self.assertLess(budget.available(due-1), 192)
            self.assertEqual(budget.wait_ns(192, due-1), 1)
            self.assertEqual(budget.available(due), 192)
            budget.consume(192, due)
            self.assertEqual(budget.snapshot()['charged_bytes'], total)
        self.assertEqual(budget.snapshot()['maximum_release_bytes'], 192)

    def test_duplex_and_channel_budgets_are_independent(self):
        budgets = [SerialBaudBudget(1000,10) for _ in range(4)]
        for budget in budgets:budget.start(0)
        budgets[0].consume(10,100_000_000)
        self.assertEqual(budgets[0].available(100_000_000),0)
        for budget in budgets[1:]:self.assertEqual(budget.available(100_000_000),10)

    def test_partial_acceptance_retains_unused_credit_and_original_busy_epoch(self):
        budget = SerialBaudBudget(1000,10)
        budget.start(1_000_000)
        self.assertEqual(budget.available(101_000_000),10)
        budget.consume(3,101_000_000)
        self.assertEqual(budget.available(101_000_000),7)
        # EAGAIN/zero acceptance makes no consume call and cannot spend credit.
        self.assertEqual(budget.available(101_000_000),7)
        budget.consume(2,101_000_000)
        budget.consume(5,101_000_000)
        self.assertEqual(budget.snapshot()['charged_bytes'],10)
        self.assertEqual(budget.snapshot()['active_since_ns'],1_000_000)

    def test_idle_and_stop_resets_cannot_fund_an_instant_new_burst(self):
        budget = SerialBaudBudget(1000,10)
        budget.start(0);budget.consume(10,100_000_000)
        budget.reset(100_000_000)
        self.assertEqual(budget.available(9_000_000_000),0)
        budget.start(9_000_000_000)
        self.assertEqual(budget.available(9_000_000_000),0)
        self.assertEqual(budget.wait_ns(1,9_000_000_000),10_000_000)
        self.assertEqual(budget.available(9_010_000_000),1)
        budget.reset(9_010_000_000)
        self.assertIsNone(budget.snapshot()['active_since_ns'])
        self.assertEqual(budget.snapshot()['charged_bytes'],10)

    def test_half_stop_bit_frames_have_exact_integer_service_time(self):
        budget = SerialBaudBudget(1500,7.5)
        budget.start(0)
        self.assertEqual(budget.wait_ns(1,0),5_000_000)
        self.assertEqual(budget.available(4_999_999),0)
        self.assertEqual(budget.available(5_000_000),1)
        budget.consume(1,5_000_000)

    def test_stall_cannot_fund_multiple_batches_or_freshly_arrived_bytes(self):
        budget = SerialBaudBudget(1000,10,8)
        budget.start(0,8)
        budget.start(100_000_000,16)  # Old eight bytes waited; eight new bytes arrive now.
        budget.consume(8,100_000_000)
        self.assertEqual(budget.available(100_000_000),0)
        self.assertEqual(budget.wait_ns(8,100_000_000),80_000_000)
        self.assertGreater(budget.snapshot()['discarded_wire_time_ns'],0)
        self.assertGreater(budget.snapshot()['credit_overrun_events'],0)
        budget.reset(100_000_000)  # Expired queue records cannot transfer their clock credit.
        budget.start(100_000_000,8)
        self.assertEqual(budget.available(100_000_000),0)
        budget.consume(8,180_000_000)
        self.assertEqual(budget.snapshot()['charged_bytes'],16)

    def test_backwards_clock_or_excess_charge_is_rejected(self):
        budget = SerialBaudBudget(1000,10);budget.start(0)
        with self.assertRaises(ValueError):budget.consume(1,9_999_999)
        self.assertEqual(budget.available(10_000_000),1)
        with self.assertRaises(ValueError):budget.available(9_999_999)
        with self.assertRaises(ValueError):budget.reset(9_999_999)
        self.assertEqual(budget.snapshot()['charged_bytes'],0)
        with self.assertRaises(ValueError):budget.wait_ns(193,10_000_000)

    def test_invalid_rates_frames_sizes_and_clock_types_are_not_ignored(self):
        for arguments in ((True,10,192),(0,10,192),(115200,True,192),(115200,float('nan'),192),
                          (115200,float('inf'),192),(115200,1e308,192),(115200,10.1,192),
                          (115200,10,0),(115200,10,193)):
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):SerialBaudBudget(*arguments)
        for value in (True,-1,1.5):
            with self.subTest(now=value),self.assertRaises(ValueError):SerialBaudBudget(1000,10).start(value)


if __name__ == '__main__':
    unittest.main()
