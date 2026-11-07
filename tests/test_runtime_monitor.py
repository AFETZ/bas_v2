from pathlib import Path
import tempfile
import unittest

from scripts.product.town01_runtime_monitor import host_record


class RuntimeMonitorTests(unittest.TestCase):
    def test_missing_pressure_is_unknown_not_zero_load(self):
        with tempfile.TemporaryDirectory() as directory:
            record = host_record(Path(directory))
        self.assertEqual(record['pressure'], dict(cpu=None, io=None, memory=None))
        self.assertIsNone(record['memory_available_bytes'])
        self.assertGreater(record['monotonic_ns'], 0)
        self.assertGreater(record['unix_ns'], 0)

    def test_pressure_and_available_memory_are_preserved_with_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pressure').mkdir()
            (root / 'pressure/io').write_text('full avg10=1.20 avg60=0.50 avg300=0.10 total=1234\n')
            (root / 'meminfo').write_text('MemAvailable:    2000 kB\n')
            record = host_record(root)
        self.assertIn('total=1234', record['pressure']['io'])
        self.assertEqual(record['memory_available_bytes'], 2048000)


if __name__ == '__main__':
    unittest.main()
