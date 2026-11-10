"""Delivery resource integrity and archive containment regressions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fetch_runtime_assets',
    ROOT / 'scripts/product/fetch_runtime_assets.py')
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)


class ResourceIntegrity(unittest.TestCase):
    def test_equal_length_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'asset'
            path.write_bytes(b'abd')
            record = dict(bytes=3, sha256=hashlib.sha256(b'abc').hexdigest())
            self.assertFalse(assets.verified(path, record))

    def test_existing_wrong_asset_is_preserved_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            target = cache / 'asset'
            target.write_bytes(b'bad')
            record = dict(name='asset', bytes=3, sha256=hashlib.sha256(b'abc').hexdigest())
            with patch.object(assets.urllib.request, 'urlopen') as request:
                with self.assertRaisesRegex(ValueError, 'left untouched'):
                    assets.fetch(record, 'example/repo', 'v1', cache)
                request.assert_not_called()
            self.assertEqual(target.read_bytes(), b'bad')

    def test_cache_symlink_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'real').write_bytes(b'abc')
            (path / 'asset').symlink_to(path / 'real')
            record = dict(bytes=3, sha256=hashlib.sha256(b'abc').hexdigest())
            self.assertFalse(assets.verified(path / 'asset', record))

    def test_wheel_archive_traversal_and_links_are_rejected_before_extract(self):
        for name, kind in [('native-runtime-wheels/../../escape', tarfile.REGTYPE),
                           ('native-runtime-wheels/link', tarfile.SYMTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / '.external').mkdir()
                archive = root / 'wheels.tar'
                with tarfile.open(archive, 'w') as package:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    if kind == tarfile.SYMTYPE:
                        entry.linkname = '/etc/passwd'
                    package.addfile(entry)
                manifest = dict(image_id='unused', native_wheels=dict(
                    output='.external/native-runtime-wheels',
                    repository='example/repo', release='v1'))
                with patch.object(assets, 'ROOT', root), patch.object(
                        assets, 'fetch', return_value=archive):
                    with self.assertRaisesRegex(ValueError, 'Unsafe'):
                        assets.get_wheels(manifest, root / 'cache')
                self.assertFalse((root / 'escape').exists())

    def test_changed_terrain_source_is_rejected_before_geometry_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.json').write_text('{}')
            manifest = dict(terrain=dict(source='source.json', source_sha256='0' * 64,
                                         output='.external/terrain-reference'))
            with patch.object(assets, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'input changed'):
                    assets.check_terrain(manifest)

    def test_canonical_profile_matches_preserved_successful_inputs(self):
        config = json.loads((ROOT / 'runtime-terrain.json').read_text())
        self.assertEqual(config['drone']['count'], 5)
        self.assertEqual(config['flows']['payload_load']['useful_bitrate_bps'], 10000)
        self.assertEqual(config['propagation']['radio_config'],
                         'network/config/native_wifi_fullmesh_100ms.yaml')
        manifest = json.loads(assets.MANIFEST.read_text())
        self.assertEqual(config['runtime']['image'], manifest['image_id'])
        source = ROOT / manifest['terrain']['source']
        self.assertEqual(assets.bootstrap.sha256(source),
                         manifest['terrain']['source_sha256'])


if __name__ == '__main__':
    unittest.main()
