from pathlib import Path
import tempfile
import unittest

from scripts.product.town01_runtime_monitor import host_record, cpu_partition


class RuntimeMonitorTests(unittest.TestCase):
    def topology(self, root, rows):
        for cpu,package,core in rows:
            path=root/f'cpu{cpu}'/'topology';path.mkdir(parents=True)
            (path/'physical_package_id').write_text(str(package))
            (path/'core_id').write_text(str(core))

    def test_smt_siblings_do_not_cross_roles_on_the_target_32_cpu_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            rows=[(cpu,0,cpu%16) for cpu in range(32)]
            self.topology(root,rows)
            # The old logical split actually shared eight physical cores.
            self.assertEqual({cpu%16 for cpu in range(8)} & {cpu%16 for cpu in range(8,32)},set(range(8)))
            result=cpu_partition(range(32),root)
        self.assertEqual(result['stack_cpus'],list(range(8))+list(range(16,24)))
        self.assertEqual(result['radio_cpus'],list(range(8,16))+list(range(24,32)))
        self.assertFalse({cpu%16 for cpu in result['stack_cpus']} & {cpu%16 for cpu in result['radio_cpus']})
        self.assertFalse(result['shared_physical_cores'])

    def test_noncontiguous_restricted_masks_and_duplicate_core_ids_on_two_sockets(self):
        rows=[(4,0,0),(12,0,0),(7,1,0),(15,1,0),(99,3,0)]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.topology(root,rows)
            result=cpu_partition([4,7,12,15],root)
        self.assertEqual(result['allowed_cpus'],[4,7,12,15])
        self.assertEqual(result['stack_cpus'],[4,12])
        self.assertEqual(result['radio_cpus'],[7,15])
        self.assertNotIn(99,result['stack_cpus']+result['radio_cpus'])

    def test_single_physical_core_is_explicitly_shared_and_missing_topology_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.topology(root,[(2,0,9),(6,0,9)])
            result=cpu_partition([2,6],root)
            self.assertTrue(result['shared_physical_cores'])
            self.assertEqual(result['stack_cpus'],result['radio_cpus'])
            for allowed in ([],[2,2],[True],[2,7]):
                with self.subTest(allowed=allowed),self.assertRaises(ValueError):
                    cpu_partition(allowed,root)

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
