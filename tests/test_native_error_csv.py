"""Compile the actual rare-path native CSV encoder and round-trip exception records."""
import csv
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeErrorCsvTests(unittest.TestCase):
    def test_actual_error_encoder_preserves_complete_csv_records(self):
        compiler = shutil.which('g++') or shutil.which('clang++')
        if not compiler:
            self.skipTest('C++ compiler required; pinned native image provides g++')
        source = (ROOT / 'network/ns3/scratch/upstream-sionna-tap-spike.cc').read_text()
        start = source.index('std::string\nQuoteEventCsvField(')
        stop = source.index('\n}\n', start) + len('\n}\n')
        # Compile the production function, not a second implementation of quoting.
        program = '#include <string>\n#include <iostream>\n' + source[start:stop] + r'''
int main() {
    std::string value;
    while (std::getline(std::cin, value, '\0'))
        std::cout << "0.000000,1,flight,rt_pool_failed,ns3,,0,0,0,0,,"
                  << QuoteEventCsvField(value) << '\n';
}
'''
        values = [
            '', 'ordinary diagnostic', 'packet_uid=17;protocol=17',
            'commas, must remain, in one field', 'quoted "failure"',
            'RuntimeError: Full mesh per-source candidate capacity reached; no clipped result admitted\n'
            '\nAt:\n  /sdk/sionna/rt/path_solvers/path_solver.py(195): __call__\n  <string>(53): solve\n',
            'CR\rLF\r\nLF\n"all", together', 'Unicode: Ошибка канала',
            'x' * 65536 + '\n"large", traceback', 'last ordinary record',
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cpp, exe = directory / 'probe.cc', directory / 'probe'
            cpp.write_text(program)
            subprocess.run([compiler, '-std=c++17', '-O2', str(cpp), '-o', str(exe)],
                           check=True, capture_output=True, timeout=30)
            result = subprocess.run([str(exe)], input=('\0'.join(values) + '\0').encode(),
                                    capture_output=True, check=True, timeout=10)
        text = result.stdout.decode()
        rows = list(csv.reader(io.StringIO(text, newline=''), strict=True))
        self.assertEqual(len(rows), len(values))
        self.assertTrue(all(len(row) == 12 for row in rows))
        self.assertEqual([row[-1] for row in rows], values)
        prefix = '0.000000,1,flight,rt_pool_failed,ns3,,0,0,0,0,,'
        for value in (values[0], values[1], values[2], values[-1]):
            self.assertIn(prefix + value + '\n', text)


if __name__ == '__main__':
    unittest.main()
