"""Real RAM publication of parser fixtures; no simulator or RF success is asserted."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import bas


def fixture_module(name):
    specification = importlib.util.spec_from_file_location('spool_fixture_'+name, bas.ROOT/'tests'/f'{name}.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class PublishedRuntimeVerifierTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'actual tmpfs and renameat2 publication require Linux')
    def test_all_existing_verifiers_accept_closed_physical_paths_after_ram_removal(self):
        from scripts.product import runtime_storage as storage
        from scripts.product import native_library_identity as identity
        from scripts.product.summarize_native_radio_five_uav import native_rt_worker_contract
        from network.coordination.service import Events
        from network.jsonl import AsyncJsonlWriter
        import yaml

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        run_id = 'published-parser-fixture'
        for name in ('MIN_FREE_BYTES', 'MIN_AVAILABLE_MEMORY_BYTES'):
            threshold = patch.object(storage, name, 16*1024)
            threshold.start()
            self.addCleanup(threshold.stop)
        descriptor = storage.prepare(root, run_id, 'bas-core-'+run_id)
        spool = Path(descriptor['spool'])
        self.assertEqual(spool.parent, Path('/dev/shm'))
        self.addCleanup(lambda: shutil.rmtree(spool) if spool.exists() else None)
        source = Path(descriptor['raw_source'])
        canonical = root/'runs/native-radio-realtime'/run_id
        for relative in ('metrics', 'configuration/initial', 'configuration/final'):
            (source/relative).mkdir(parents=True, exist_ok=True)

        # Reuse known parser fixtures while writing all real journals through
        # their production bounded writers at the actual physical RAM path.
        runtime = fixture_module('test_runtime').RuntimeTests()
        runtime.qualified_source_run = lambda: (source, {})
        runtime.native_library_evidence()
        expected = identity.read_json(source/identity.EXPECTED)
        expected['run_dir'] = str(source)
        (source/identity.EXPECTED).write_text(json.dumps(expected))
        observed = identity.read_json(source/identity.OBSERVED)
        for key in ('process_before', 'process_after'):
            observed[key]['argv'][-1] = f'--statsFile={source}/metrics/native_radio_stats.json'
        observed['expected_sha256'] = identity.digest(source/identity.EXPECTED)
        (source/identity.OBSERVED).write_text(json.dumps(observed))
        closed = identity.read_json(source/identity.CLOSED)
        closed.update(expected_sha256=identity.digest(source/identity.EXPECTED),
                      observed_sha256=identity.digest(source/identity.OBSERVED))
        (source/identity.CLOSED).write_text(json.dumps(closed))
        for stage in ('initial', 'final'):
            folder = source/'configuration'/stage
            relative = 'inputs/native_library_expected/value.json'
            data = (source/identity.EXPECTED).read_bytes()
            (folder/relative).write_bytes(data)
            manifest = identity.read_json(folder/'manifest.json')
            manifest['files'][0]['bytes'] = len(data)
            (folder/'manifest.json').write_text(json.dumps(manifest))
        runtime.position_journal_evidence()
        runtime.payload_journal_evidence()
        for node_id in range(6):
            name = 'gcs' if node_id == 0 else f'uav{node_id}'
            events = Events(source/f'logs/coordination_{name}.jsonl', run_id, node_id)
            events('service_started')
            events('service_stopped')
            events.close()
        with (source/'environment.txt').open('a') as stream:
            stream.write('\nuav_count=5\ncoordination_journal_mode=bounded_async_jsonl_v1\n'
                         'position_journal_mode=bounded_async_jsonl_v1\n'
                         f'runtime_storage_mode={storage.MODE}\n')
        Path(descriptor['runtime_log_source']).write_text('closed parser-fixture controller\n')

        worker = fixture_module('test_native_radio_contract').NativeRadioContractTests()
        # Urban inputs are prepared before runtime under control/<run>/generated;
        # only live producer outputs are relocated from the RAM raw directory.
        worker.root = root/'runs/control'/run_id/'generated'
        worker.root.mkdir()
        worker.scene = worker.root/'fixture-scene.xml'
        worker.scene.write_text('<scene version="3.0.0"/>')
        worker.profile = yaml.safe_load((bas.ROOT/'network/config/native_wifi_80211n_spectrum_product.yaml').read_text())
        stats, scenario = worker.evidence(pool=True, full_mesh=True)
        path, records = worker.result_journal_evidence(stats, source)
        path.unlink()
        writer = AsyncJsonlWriter(path)
        try:
            for line in records.decode('utf-8').splitlines():
                writer.write(line)
        finally:
            writer.close()
        stats['sionna_rt_workers']['result_log_writer'] = writer.snapshot()
        verifiers = dict(libraries=bas.native_library_verification, positions=bas.node_state_journal_verification,
                         payload=bas.payload_journal_verification, coordination=bas.coordination_journal_verification)
        before = {name: verify(source) for name, verify in verifiers.items()}
        before['worker'] = native_rt_worker_contract(stats, scenario, run_dir=source)
        for name, proof in before.items():
            self.assertTrue(proof['passed'], (name, proof))
        raw_hashes = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in source.rglob('*') if p.is_file()}

        closed_container = dict(verified=True, container_id=None, container_name=descriptor['container_name'],
                                container_absent=True, captured_host_monotonic_ns=time.monotonic_ns())
        with patch.object(storage, 'container_closure', return_value=closed_container):
            publication = storage.publish(root, descriptor, 0)
        self.assertTrue(publication['passed'], publication)
        self.assertFalse(spool.exists())
        self.assertFalse(canonical.is_symlink())
        self.assertEqual(publication, storage.verify(canonical))
        for relative, digest in raw_hashes.items():
            self.assertEqual(hashlib.sha256((canonical/relative).read_bytes()).hexdigest(), digest, relative)
        after = {name: verify(canonical) for name, verify in verifiers.items()}
        after['worker'] = native_rt_worker_contract(stats, scenario, run_dir=canonical)
        for name, proof in after.items():
            self.assertTrue(proof['passed'], (name, proof))
        self.assertEqual(before, after)
        self.assertEqual(identity.read_json(canonical/identity.EXPECTED)['run_dir'], str(source))
        self.assertEqual(publication['source_run_dir'], str(source))
        self.assertEqual(publication['destination_run_dir'], str(canonical))
