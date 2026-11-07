"""Public source configuration/snapshot tests; no RT or packet reception claim."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import bas
from scripts.product.capture_effective_config import capture_snapshot
from scripts.product.prepare_native_sources import UniqueSafeLoader, prepare


class InterferenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = json.loads((bas.ROOT / 'runtime.json').read_text())
        (self.root / 'scene.json').write_bytes((bas.ROOT / self.config['environment']['scene']).read_bytes())
        self.config['environment']['scene'] = 'scene.json'
        shutil.copy2(bas.ROOT / 'network/config/native_wifi_80211n_spectrum_product.yaml', self.root / 'radio.yaml')
        self.config['propagation'] = dict(model='sionna', radio_config='radio.yaml',
            ground_material='medium_dry_ground', building_material='concrete', thickness_m=.2)
        self.config['interference'] = dict(sources='sources.yaml')
        self.source = dict(id='reference_source', position_m=[0, -40, 10], orientation_rad=[0, 0],
            pattern='tr38901', gain_dbi=0, power_w=.01, center_hz=2412000000, bandwidth_hz=20000000,
            start_s=10, stop_s=20, period_s=.01, duty_cycle=1, mode='continuous', spectral_shape='rectangular')
        self.source_file = self.root / 'sources.yaml'
        self.write_source()

    def write_source(self, source=None):
        self.source_file.write_text('# authored source bytes must be preserved\n' +
            yaml.safe_dump(dict(sources=[self.source if source is None else source])), encoding='utf-8')

    def load(self, config=None):
        path = self.root / 'runtime.json'
        path.write_text(json.dumps(self.config if config is None else config))
        return bas.load_config(path, root=self.root)

    def test_old_configs_default_to_disabled_and_ambient_source_is_not_forwarded(self):
        config = copy.deepcopy(self.config)
        del config['interference']
        config['propagation'] = dict(model='geometry_logdistance')
        resolved = self.load(config)
        self.assertEqual(resolved['interference'], {'sources': None})
        env = bas.environment(resolved, 'disabled', {'BAS_NATIVE_SOURCES': 'ambient.yaml'})
        self.assertNotIn('BAS_NATIVE_SOURCES', env)
        folder = self.root / 'runs/control/disabled'
        folder.mkdir(parents=True)
        with patch.object(bas, 'ROOT', self.root):
            hashes = bas.snapshot_inputs(resolved, folder, env)
        self.assertIsNone(hashes['sources_sha256'])
        self.assertFalse((folder / 'sources.yaml').exists())
        self.assertNotIn('BAS_NATIVE_SOURCES', env)

    def test_unknown_sections_source_types_and_escaping_paths_are_rejected(self):
        for interference in (None, [], True, {}, {'source': 'sources.yaml'}, {'sources': None, 'extra': 1}):
            config = copy.deepcopy(self.config)
            config['interference'] = interference
            with self.subTest(interference=interference), self.assertRaises(ValueError):
                self.load(config)
        for name in ('../outside.yaml', '/absolute.yaml', 'C:/outside.yaml', 'sources.json', '', True, []):
            config = copy.deepcopy(self.config)
            config['interference']['sources'] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(config)

    def test_geometry_cannot_silently_ignore_an_enabled_source(self):
        self.config['propagation'] = dict(model='geometry_logdistance')
        with self.assertRaisesRegex(ValueError, 'requires propagation.model=sionna'):
            self.load()

    def test_runner_rejects_geometry_sources_before_python_docker_or_runtime_start(self):
        bash = (str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe')
                if os.name == 'nt' else shutil.which('bash'))
        if not bash or not Path(bash).is_file():
            self.skipTest('Bash is required to execute the runner guard check')
        env = {key: value for key, value in os.environ.items() if not key.startswith('BAS_')}
        env.update(BAS_NATIVE_PROPAGATION_PROFILE='geometry_logdistance', BAS_NATIVE_SOURCES='not-read.yaml')
        # These sentinel functions prevent any accidental simulator/bootstrap
        # execution if the guard regresses. No Docker daemon is contacted.
        wrapper = ('python3() { printf unexpected_python >&2; return 95; }; '
                   'docker() { printf unexpected_docker >&2; return 96; }; '
                   'export -f python3 docker; exec bash "$1"')
        result = subprocess.run([bash, '-c', wrapper, 'guard-test',
            str(bas.ROOT / 'network/ns3/run_native_radio_five_uav.sh')], cwd=bas.ROOT,
            env=env, capture_output=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('not installed by geometry_logdistance', result.stderr)
        self.assertNotIn('unexpected_', result.stderr)

    def test_source_yaml_duplicates_malformed_values_and_unknown_fields_fail_before_runtime(self):
        valid = self.source_file.read_text()
        for text in (valid + '\nsources: []\n', 'sources: [invalid',
                     yaml.safe_dump(dict(sources=[{**self.source, 'ignored': 1}])),
                     yaml.safe_dump(dict(sources=[{**self.source, 'power_w': True}]))):
            self.source_file.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load()

    def test_all_sweep_centers_must_be_valid_for_the_selected_itu_materials(self):
        source = {**self.source, 'mode': 'sweep', 'sweep_hz': [2412000000, 20000000000], 'dwell_s': 5}
        del source['center_hz']
        self.write_source(source)
        with self.assertRaisesRegex(ValueError, 'ITU validity'):
            self.load()
        # A different, non-overlapping RF carrier remains legitimate input:
        # native spectrum overlap decides interference, not this config loader.
        source['sweep_hz'] = [2412000000, 5800000000]
        self.write_source(source)
        self.assertEqual(self.load()['interference']['sources'], 'sources.yaml')

    def test_windows_separators_are_normalized_for_the_linux_runtime(self):
        (self.root / 'sources').mkdir()
        shutil.copy2(self.source_file, self.root / 'sources/reference.yml')
        self.config['interference']['sources'] = 'sources\\reference.yml'
        self.assertEqual(self.load()['interference']['sources'], 'sources/reference.yml')

    def test_only_validated_frozen_bytes_and_hash_reach_the_runtime(self):
        resolved = self.load()
        original = self.source_file.read_bytes()
        env = bas.environment(resolved, 'source-test', {'BAS_NATIVE_SOURCES': 'ambient.yaml'})
        self.assertNotIn('BAS_NATIVE_SOURCES', env)
        folder = self.root / 'runs/control/source-test'
        folder.mkdir(parents=True)
        with patch.object(bas, 'ROOT', self.root):
            hashes = bas.snapshot_inputs(resolved, folder, env)
        frozen = self.root / env['BAS_NATIVE_SOURCES']
        self.assertEqual(env['BAS_NATIVE_SOURCES'], 'runs/control/source-test/sources.yaml')
        self.assertEqual(frozen.read_bytes(), original)
        self.assertEqual(hashes['sources_sha256'], hashlib.sha256(original).hexdigest())
        self.source_file.write_text('sources: [malformed')
        self.assertEqual(frozen.read_bytes(), original)
        self.assertEqual(prepare(yaml.load(frozen.read_text(), Loader=UniqueSafeLoader))['segments'][0]['id'], 'reference_source')
        # Validation at UI time is insufficient if an author edits the source.
        other = self.root / 'runs/control/changed-source'
        other.mkdir()
        with patch.object(bas, 'ROOT', self.root), self.assertRaises(ValueError):
            bas.snapshot_inputs(resolved, other, {})

    def test_effective_capture_records_raw_input_and_existing_resolved_schedule_once(self):
        run = self.root / 'raw-run'
        (run / 'logs').mkdir(parents=True)
        prepared = prepare(yaml.load(self.source_file.read_text(), Loader=UniqueSafeLoader))
        (run / 'logs/native_sources.json').write_text(json.dumps(prepared))
        result = capture_snapshot(run, stage='initial', repo_root=bas.ROOT,
            configs=[('native_sources_input', self.source_file)], observed=[])
        records = {row['label']: row for row in result['files']}
        self.assertEqual(records['native_sources']['status'], 'observed')
        self.assertEqual(records['native_sources_input']['status'], 'observed')
        self.assertEqual(len(records), len(result['files']))
        copied = run / 'configuration/initial' / records['native_sources_input']['copied_path']
        self.assertEqual(copied.read_bytes(), self.source_file.read_bytes())


if __name__ == '__main__':
    unittest.main()
