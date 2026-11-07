"""Build preparation/integrity tests. These do not qualify Docker or flights."""
import json
import contextlib
import io
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.product import bootstrap_native_runtime as bootstrap


IMAGE = 'sha256:' + 'a' * 64
PYTHON = dict(major_minor='3.10', version='3.10.12', machine='x86_64', platform='linux', pip='22.0.2')


class NativeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in bootstrap.INPUTS:
            p = self.root / name
            p.parent.mkdir(exist_ok=True, parents=True)
            p.write_bytes((bootstrap.ROOT / name).read_bytes())
        self.bundle = self.root / '.external/wheels'
        (self.bundle / 'wheels').mkdir(parents=True)
        for name, version in bootstrap.direct_pins(self.root).items():
            self.wheel(name, version)
        self.wheel('transitive-test', '1.2.3')
        self.manifest = bootstrap.freeze_wheels(self.root, self.bundle, IMAGE, PYTHON)

    def wheel(self, name, version):
        p = self.bundle / 'wheels' / f'{name.replace("-", "_")}-{version}-py3-none-any.whl'
        with zipfile.ZipFile(p, 'w') as archive:
            archive.writestr(f'{name}-{version}.dist-info/METADATA',
                            f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        return p

    def test_full_resolved_bundle_contains_transitive_hash_and_separate_tooling(self):
        result = bootstrap.validate_bundle(self.root, self.bundle, IMAGE)
        runtime = (self.bundle / bootstrap.LOCK_NAMES[0]).read_text()
        tooling = (self.bundle / bootstrap.LOCK_NAMES[1]).read_text()
        self.assertIn('transitive-test==1.2.3 --hash=sha256:', runtime)
        self.assertNotIn('cmake==', runtime)
        self.assertEqual(len(tooling.splitlines()), 1)
        self.assertTrue(tooling.startswith('cmake==3.31.6 --hash=sha256:'))
        self.assertEqual(result['image_id'], IMAGE)

    def test_tampered_wheel_is_rejected(self):
        p = self.bundle / 'wheels/transitive_test-1.2.3-py3-none-any.whl'
        with zipfile.ZipFile(p, 'a') as archive:
            archive.writestr('unexpected.py', 'changed')
        with self.assertRaisesRegex(ValueError, 'contents/hash'):
            bootstrap.validate_bundle(self.root, self.bundle, IMAGE)

    def test_missing_transitive_wheel_is_rejected(self):
        (self.bundle / 'wheels/transitive_test-1.2.3-py3-none-any.whl').unlink()
        with self.assertRaisesRegex(ValueError, 'contents/hash'):
            bootstrap.validate_bundle(self.root, self.bundle, IMAGE)

    def test_changed_lock_is_rejected(self):
        (self.bundle / bootstrap.LOCK_NAMES[0]).write_text('sionna==1.2.2\n')
        with self.assertRaisesRegex(ValueError, 'lock content'):
            bootstrap.validate_bundle(self.root, self.bundle, IMAGE)

    def test_changed_direct_input_requires_new_resolution(self):
        with (self.root / bootstrap.INPUTS[0]).open('a') as stream:
            stream.write('extra==1.0\n')
        with self.assertRaisesRegex(ValueError, 'input hashes'):
            bootstrap.validate_bundle(self.root, self.bundle, IMAGE)

    def test_base_sionna_version_cannot_satisfy_native_pins(self):
        (self.bundle / 'wheels/sionna_rt-1.2.0-py3-none-any.whl').unlink()
        self.wheel('sionna-rt', '1.2.2')
        with self.assertRaisesRegex(ValueError, 'direct pin'):
            bootstrap.freeze_wheels(self.root, self.bundle, IMAGE, PYTHON)

    def test_duplicate_distribution_is_rejected(self):
        self.wheel('sionna', '1.2.2')
        with self.assertRaisesRegex(ValueError, 'one wheel'):
            bootstrap.freeze_wheels(self.root, self.bundle, IMAGE, PYTHON)

    def test_foreign_python_platform_image_are_rejected(self):
        for key, value in [('major_minor', '3.12'), ('machine', 'aarch64'), ('platform', 'win32')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bootstrap.freeze_wheels(self.root, self.bundle, IMAGE, dict(PYTHON, **{key: value}))
        with self.assertRaisesRegex(ValueError, 'schema/image'):
            bootstrap.validate_bundle(self.root, self.bundle, 'sha256:' + 'b' * 64)

    def test_invalid_wheel_and_unpinned_requirement_are_rejected(self):
        malformed = self.bundle / 'wheels/empty.whl'
        with zipfile.ZipFile(malformed, 'w'):
            pass
        with self.assertRaisesRegex(ValueError, 'METADATA'):
            bootstrap.wheel_record(malformed)
        (self.root / bootstrap.INPUTS[0]).write_text('sionna>=1.2\n')
        with self.assertRaisesRegex(ValueError, 'exact dependency pin'):
            bootstrap.direct_pins(self.root)

    def test_offline_install_uses_hashes_and_resolves_closure_without_base_fallback(self):
        script = bootstrap.install_script('.external/a b;echo unwanted', '.external/a b/target', bootstrap.LOCK_NAMES[0])
        argv = shlex.split(script)
        for flag in ['--require-hashes', '--no-index', '--ignore-installed', '--only-binary=:all:']:
            self.assertIn(flag, argv)
        self.assertNotIn('--no-deps', argv)  # Missing closure must fail in pip, not pass silently.
        self.assertEqual(argv[argv.index('--find-links') + 1], '.external/a b;echo unwanted/wheels')

    def test_environment_check_reads_private_directory_not_global_metadata(self):
        script = bootstrap.environment_script('.external/private', {'sionna-rt': '1.2.0'})
        self.assertIn('distributions(path=[p])', script)
        self.assertIn('sys.version_info[:2] == (3,10)', script)
        self.assertNotIn('import sionna', script)  # Check-only does not initialize CUDA/LLVM.

    def test_build_docker_has_canonical_mount_no_gpu_privilege_or_runtime(self):
        args = bootstrap.docker_command(self.root, IMAGE, 'echo build-only', readonly=True)
        self.assertIn(f'{self.root.resolve()}:/workspace/multiagent_simulation:ro', args)
        self.assertIn('--read-only', args)
        self.assertEqual(args[args.index('--network') + 1], 'none')
        for forbidden in ['--gpus', '--privileged', '--network=host', '-p']:
            self.assertNotIn(forbidden, args)
        self.assertNotIn('run_native_radio_five_uav.sh', ' '.join(args))

    def test_workspace_path_traversal_and_symlink_escape_are_rejected(self):
        for value in ['../escape', str(self.root.resolve()), 'nested/../../escape', '.']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootstrap.workspace_path(self.root, value)

    def test_resolve_refuses_existing_bundle_before_docker(self):
        with patch.object(bootstrap, 'container') as container:
            with self.assertRaisesRegex(ValueError, 'existing wheel bundle'):
                bootstrap.resolve_lock(self.root, self.bundle, IMAGE)
            container.assert_not_called()

    def test_check_only_reports_missing_inputs_without_creating_workspace_outputs(self):
        self.bundle.rename(self.bundle.with_name('saved-wheels'))
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        with patch.object(bootstrap, 'image_id', side_effect=OSError('Docker unavailable')):
            result = bootstrap.inspect(self.root, IMAGE, self.bundle)
        self.assertFalse(result['ready'])
        self.assertIn('wheel_bundle', {r['component'] for r in result['gaps']})
        self.assertIn('image', {r['component'] for r in result['gaps']})
        self.assertFalse(result['runtime_started'])
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_unmanaged_ros_outputs_are_preserved(self):
        (self.root / 'install').mkdir()
        marker = self.root / 'install/user-change'
        marker.write_text('keep this')
        with patch.object(bootstrap, 'container', return_value=''), self.assertRaisesRegex(ValueError, 'Unmanaged'):
            bootstrap.bootstrap(self.root, IMAGE, self.bundle)
        self.assertEqual(marker.read_text(), 'keep this')
        self.assertFalse((self.root / bootstrap.SDK_PATH).exists())

    def test_unmanaged_private_environment_is_preserved(self):
        private = self.root / bootstrap.SDK_PATH / '.python-deps-py310'
        private.mkdir(parents=True)
        p = private / 'user-module.py'
        p.write_text('keep this')
        with self.assertRaisesRegex(ValueError, 'Unmanaged private'):
            bootstrap.bootstrap(self.root, IMAGE, self.bundle)
        self.assertEqual(p.read_text(), 'keep this')
        self.assertFalse((self.root / bootstrap.RECEIPT).exists())

    def test_private_tree_hash_detects_code_change_and_ignores_bytecode(self):
        private = self.root / 'installed'
        private.mkdir()
        module = private / 'module.py'
        module.write_text('original')
        first = bootstrap.environment_hash(private)
        (private / '__pycache__').mkdir()
        (private / '__pycache__/module.pyc').write_bytes(b'generated')
        self.assertEqual(first, bootstrap.environment_hash(private))
        module.write_text('user edit')
        self.assertNotEqual(first, bootstrap.environment_hash(private))

    def test_previously_built_ros_user_edit_is_preserved(self):
        directory = self.root / 'install'
        directory.mkdir()
        p = directory / 'launch.py'
        p.write_text('built version')
        receipt = dict(inputs={}, status='built',
                       ros_trees={'install': bootstrap.environment_hash(directory, allow_links=True)})
        (self.root / bootstrap.RECEIPT).write_text(json.dumps(receipt))
        p.write_text('user change')
        with patch.object(bootstrap, 'input_identity', return_value={}), \
             patch.object(bootstrap, 'container', return_value=''), \
             patch.object(bootstrap, 'ensure_sdk') as sdk:
            with self.assertRaisesRegex(ValueError, 'ROS outputs changed'):
                bootstrap.bootstrap(self.root, IMAGE, self.bundle)
            sdk.assert_not_called()
        self.assertEqual(p.read_text(), 'user change')

    def test_copied_image_recipe_has_every_copy_source(self):
        root = bootstrap.ROOT / '.devcontainer'
        for line in (root / 'Dockerfile').read_text().splitlines():
            if line.startswith('COPY '):
                source = shlex.split(line)[1]
                self.assertTrue((root / source).is_file(), source)

    def test_cache_requires_canonical_in_container_source_directory(self):
        cache = self.root / bootstrap.SDK_PATH / 'cmake-cache/CMakeCache.txt'
        cache.parent.mkdir(parents=True)
        cache.write_text('CMAKE_HOME_DIRECTORY:INTERNAL=/home/bas/old-copy\n')
        before = cache.read_bytes()
        with self.assertRaisesRegex(ValueError, 'canonical SDK mount'):
            bootstrap.verify_cache(self.root)
        self.assertEqual(cache.read_bytes(), before)
        cache.write_text(f'CMAKE_HOME_DIRECTORY:INTERNAL={bootstrap.MOUNT}/{bootstrap.SDK_PATH}\n')
        bootstrap.verify_cache(self.root)

    def test_all_ten_runtime_patches_and_both_native_targets_are_prepared(self):
        self.assertEqual(len(bootstrap.PATCH_NAMES), 10)
        self.assertEqual(len(set(bootstrap.PATCH_NAMES)), 10)
        runner = (bootstrap.ROOT / 'network/ns3/run_native_radio_five_uav.sh').read_text()
        for name in bootstrap.PATCH_NAMES + bootstrap.SCRATCH_FILES:
            self.assertTrue(name in runner, f'Runner does not include {name}')
            folder = 'patches' if name.endswith('.patch') else 'scratch'
            self.assertTrue((bootstrap.ROOT / 'network/ns3' / folder / name).is_file())

    def test_mutable_image_name_is_rejected_without_docker(self):
        with patch.object(bootstrap, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'exact sha256'):
                bootstrap.image_id('multiagent_simulation:latest')
            command.assert_not_called()

    def test_failed_real_subprocess_preserves_complete_stdout_stderr_and_exit_code(self):
        log = self.root / 'attempt.log'
        code = 'import sys;print("A"*25000);print("B"*25000,file=sys.stderr);sys.exit(7)'
        with self.assertRaises(RuntimeError):
            bootstrap.command([sys.executable, '-c', code], log=log)
        text = log.read_text()
        self.assertIn('A' * 25000, text)
        self.assertIn('B' * 25000, text)
        records = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
        self.assertEqual(records[0]['argv'], [sys.executable, '-c', code])
        self.assertEqual(records[-1]['returncode'], 7)
        self.assertGreaterEqual(records[-1]['elapsed_ns'], 0)
        self.assertEqual(records[0]['source_sha256'], bootstrap.sha256(Path(bootstrap.__file__)))

    def test_interrupted_subprocess_retains_partial_output(self):
        log = self.root / 'interrupted.log'
        def interrupted(argv, **kwargs):
            kwargs['stdout'].write(b'partial-before-interrupt')
            kwargs['stderr'].write(b'partial-stderr')
            raise KeyboardInterrupt
        with patch.object(bootstrap.subprocess, 'run', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            bootstrap.command(['fixture'], log=log)
        text = log.read_text()
        self.assertIn('partial-before-interrupt', text)
        self.assertIn('partial-stderr', text)
        end = json.loads(text.splitlines()[-1])
        self.assertIsNone(end['returncode'])
        self.assertEqual(end['exception'], 'KeyboardInterrupt')

    def test_direct_commands_do_not_create_global_logs(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        with patch.object(bootstrap, 'ACTIVE_LOG', None), patch.object(bootstrap, 'ROOT', self.root):
            self.assertEqual(bootstrap.command([sys.executable, '-c', 'print("read-only helper")']), 'read-only helper')
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_container_interrupt_removes_only_exact_owned_cid(self):
        own, unrelated = 'a' * 64, 'b' * 64
        calls, cidpaths = [], []
        def fake_command(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'run':
                path = Path(argv[argv.index('--cidfile') + 1])
                self.assertFalse(path.exists())
                cidpaths.append(path)
                path.write_text(own)
                raise KeyboardInterrupt
            self.assertEqual(argv, ['docker', 'container', 'rm', '--force', own])
            self.assertFalse(kwargs['check'])
            return ''
        with patch.object(bootstrap, 'command', side_effect=fake_command), self.assertRaises(KeyboardInterrupt):
            bootstrap.container(self.root, IMAGE, 'fixture')
        self.assertEqual(len(calls), 2)
        self.assertNotIn(unrelated, [arg for call in calls for arg in call])
        self.assertFalse(cidpaths[0].parent.exists())

    def test_container_cidfiles_are_unique_and_missing_cid_never_triggers_global_cleanup(self):
        paths = []
        def fake_command(argv, **kwargs):
            self.assertEqual(argv[1], 'run')
            paths.append(argv[argv.index('--cidfile') + 1])
            raise RuntimeError('failed before creation')
        with patch.object(bootstrap, 'command', side_effect=fake_command):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'before creation'):
                    bootstrap.container(self.root, IMAGE, 'fixture')
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0], paths[1])

    def test_invalid_cid_is_never_used_for_cleanup(self):
        calls = []
        def fake_command(argv, **kwargs):
            calls.append(argv)
            Path(argv[argv.index('--cidfile') + 1]).write_text('--all')
            return ''
        with patch.object(bootstrap, 'command', side_effect=fake_command), self.assertRaisesRegex(ValueError, 'unchecked cleanup'):
            bootstrap.container(self.root, IMAGE, 'fixture')
        self.assertEqual(len(calls), 1)

    def test_cli_term_cleans_container_before_unlock_and_appends_failed_attempts(self):
        events = []
        previous_handler = signal.getsignal(signal.SIGTERM)
        @contextlib.contextmanager
        def lock():
            try:
                yield
            finally:
                events.append('unlock')
        def fake_command(argv, **kwargs):
            if argv[1] == 'run':
                Path(argv[argv.index('--cidfile') + 1]).write_text('c' * 64)
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertEqual(argv, ['docker', 'container', 'rm', '--force', 'c' * 64])
            events.append('cleanup')
            return ''
        def build(*args):
            return bootstrap.container(self.root, IMAGE, 'fixture')
        with patch.object(bootstrap, 'ROOT', self.root), patch.object(bootstrap.sys, 'platform', 'linux'), \
             patch.object(bootstrap.os, 'getuid', return_value=1000, create=True), \
             patch.object(bootstrap.os, 'getgid', return_value=1000, create=True), \
             patch.object(bootstrap, 'runtime_lock', lock), patch.object(bootstrap, 'image_id', return_value=IMAGE), \
             patch.object(bootstrap, 'bootstrap', side_effect=build), \
             patch.object(bootstrap, 'command', side_effect=fake_command), contextlib.redirect_stderr(io.StringIO()):
            for _ in range(2):
                self.assertEqual(bootstrap.main(['--bootstrap', '--image', IMAGE]), 143)
        self.assertEqual(events, ['cleanup', 'unlock', 'cleanup', 'unlock'])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
        self.assertIsNone(bootstrap.ACTIVE_LOG)
        records = [json.loads(line) for line in (self.root / '.external/native-bootstrap.log').read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['attempt_start', 'attempt_end'] * 2)
        self.assertTrue(all(r['status'] == 'cancelled' for r in records if r['event'] == 'attempt_end'))


@unittest.skipUnless(shutil.which('git'), 'Real Git is required for index preservation regression')
class NativeSdkPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / bootstrap.SDK_PATH
        self.sdk.mkdir(parents=True)
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'Bootstrap fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.symlinks', 'false')
        (self.sdk / 'source').write_text('value=0\nuntouched\noutside=original\n', newline='\n')
        for name in bootstrap.HISTORICAL_MISSING_NON_RUNTIME:
            path = self.sdk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('non-runtime fixture\n', newline='\n')
        for name in bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS:
            path = self.sdk / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('../figures', newline='\n')
        self.git('add', '.')
        # Git's no-symlink checkout representation works on Windows without
        # requiring developer mode. The committed original type is still 120000.
        for name in bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS:
            blob = self.git('hash-object', '-w', name)
            self.git('update-index', '--cacheinfo', f'120000,{blob},{name}')
        self.git('commit', '--quiet', '-m', 'fixture')
        self.head = self.git('rev-parse', 'HEAD')
        for number, path in enumerate(bootstrap.patches(self.root), 1):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('diff --git a/source b/source\n--- a/source\n+++ b/source\n'
                            f'@@ -1 +1 @@\n-value={number - 1}\n+value={number}\n', newline='\n')
            self.git('apply', '--unidiff-zero', str(path))
        # Production patches have context. Add context to each test patch after applying.
        for path in bootstrap.patches(self.root):
            text = path.read_text().replace('@@ -1 +1 @@', '@@ -1,2 +1,2 @@') + ' untouched\n'
            path.write_text(text, newline='\n')

    def git(self, *args):
        return bootstrap.command(['git', '-C', self.sdk, *args])

    def verify_preserving_git(self):
        index_path = self.sdk / '.git/index'
        index = index_path.read_bytes() if index_path.exists() else None
        objects = sorted(str(p.relative_to(self.sdk)) for p in (self.sdk / '.git/objects').rglob('*'))
        try:
            with patch.object(bootstrap, 'NS3_SHA', self.head):
                return bootstrap.verify_sdk(self.root, self.sdk)
        finally:
            if index is None:
                self.assertFalse(index_path.exists())
                self.assertFalse(index_path.is_symlink())
            else:
                self.assertEqual(index, index_path.read_bytes())
            self.assertEqual(objects, sorted(str(p.relative_to(self.sdk)) for p in (self.sdk / '.git/objects').rglob('*')))

    def test_exact_patched_tree_passes_without_changing_user_index(self):
        result = self.verify_preserving_git()
        self.assertEqual(result['ns3_sha'], self.head)
        self.assertEqual(result['verification_scope'], 'exact_patched_tree')
        self.assertEqual(result['allowed_missing_non_runtime'], [])
        self.assertEqual(result['expanded_non_runtime_doc_symlinks'], [])
        self.assertEqual(result['git_index_status'], 'present_verified')
        self.assertEqual(self.git('diff', '--cached'), '')

    def test_only_explicit_absent_non_runtime_paths_pass_and_are_reported(self):
        for name in bootstrap.HISTORICAL_MISSING_NON_RUNTIME:
            (self.sdk / name).unlink()
        result = self.verify_preserving_git()
        self.assertEqual(result['allowed_missing_non_runtime'], sorted(bootstrap.HISTORICAL_MISSING_NON_RUNTIME))
        self.assertEqual(result['verification_scope'], 'exact_patched_tree_except_listed_missing_non_runtime_files')
        self.assertTrue(all(not (self.sdk / p).exists() for p in bootstrap.HISTORICAL_MISSING_NON_RUNTIME))

    def test_historical_four_missing_files_and_two_expanded_doc_links_are_reported(self):
        for name in bootstrap.HISTORICAL_MISSING_NON_RUNTIME:
            (self.sdk / name).unlink()
        for name in bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS:
            path = self.sdk / name
            path.unlink()
            path.mkdir()
            (path / 'archive-content.txt').write_text('Not verified as original documentation')
        result = self.verify_preserving_git()
        self.assertEqual(result['allowed_missing_non_runtime'], sorted(bootstrap.HISTORICAL_MISSING_NON_RUNTIME))
        self.assertEqual(result['expanded_non_runtime_doc_symlinks'], sorted(bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS))
        self.assertEqual(result['verification_scope'], 'exact_patched_tree_except_listed_non_runtime_packaging')
        self.assertTrue(all((self.sdk / p / 'archive-content.txt').exists()
                            for p in bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS))

    def test_doc_link_absence_without_expanded_directory_is_rejected(self):
        name = sorted(bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS)[0]
        (self.sdk / name).unlink()
        with self.assertRaisesRegex(RuntimeError, name):
            self.verify_preserving_git()

    def test_same_doc_directory_name_is_rejected_if_head_was_not_a_symlink(self):
        name = sorted(bootstrap.HISTORICAL_EXPANDED_DOC_SYMLINKS)[0]
        blob = self.git('hash-object', '-w', name)
        self.git('update-index', '--cacheinfo', f'100644,{blob},{name}')
        self.git('commit', '--quiet', '-m', 'ordinary file, not original symlink')
        self.head = self.git('rev-parse', 'HEAD')
        (self.sdk / name).unlink()
        (self.sdk / name).mkdir()
        with self.assertRaisesRegex(RuntimeError, name):
            self.verify_preserving_git()

    def test_unknown_code_deletion_is_rejected_and_not_restored(self):
        (self.sdk / 'source').unlink()
        with self.assertRaisesRegex(RuntimeError, 'D source'):
            self.verify_preserving_git()
        self.assertFalse((self.sdk / 'source').exists())

    def test_edited_known_non_runtime_file_is_not_excluded(self):
        path = self.sdk / 'AGENTS.md'
        path.write_text('user edit\n', newline='\n')
        with self.assertRaisesRegex(RuntimeError, 'M AGENTS.md'):
            self.verify_preserving_git()
        self.assertEqual(path.read_text(), 'user edit\n')

    def test_known_file_replaced_by_directory_is_not_an_allowed_absence(self):
        path = self.sdk / 'AGENTS.md'
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'AGENTS.md'):
            self.verify_preserving_git()
        self.assertTrue(path.is_dir())

    def test_staged_only_user_code_edit_is_rejected_and_preserved(self):
        path = self.sdk / 'source'
        approved = path.read_bytes()
        path.write_text(path.read_text().replace('outside=original', 'outside=staged edit'), newline='\n')
        self.git('add', 'source')
        path.write_bytes(approved)  # Actual runtime tree alone now matches.
        with self.assertRaises(RuntimeError):
            self.verify_preserving_git()
        self.assertEqual(path.read_bytes(), approved)
        self.assertNotEqual(self.git('diff', '--cached'), '')

    def test_staging_exact_approved_patches_is_accepted(self):
        self.git('add', 'source')
        result = self.verify_preserving_git()
        self.assertEqual(result['allowed_missing_non_runtime'], [])

    def test_source_archive_without_index_passes_without_creating_an_index(self):
        index_path = self.sdk / '.git/index'
        backup = index_path.read_bytes()
        self.assertTrue(backup.startswith(b'DIRC'))
        index_path.unlink()
        result = self.verify_preserving_git()
        self.assertEqual(result['git_index_status'], 'absent_in_source_archive')
        self.assertFalse(index_path.exists())

    def test_missing_index_does_not_bypass_actual_source_edit(self):
        (self.sdk / '.git/index').unlink()
        path = self.sdk / 'source'
        path.write_text(path.read_text().replace('outside=original', 'outside=edited'), newline='\n')
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'M source'):
            self.verify_preserving_git()
        self.assertEqual(path.read_bytes(), before)

    def test_existing_valid_empty_index_is_not_treated_as_missing(self):
        self.git('read-tree', '--empty')
        self.assertTrue((self.sdk / '.git/index').read_bytes().startswith(b'DIRC'))
        with self.assertRaises(RuntimeError):
            self.verify_preserving_git()

    def test_existing_corrupt_empty_index_is_not_treated_as_missing(self):
        (self.sdk / '.git/index').write_bytes(b'')
        with self.assertRaises(RuntimeError):
            self.verify_preserving_git()

    def test_extra_user_edit_outside_patch_is_rejected_and_preserved(self):
        p = self.sdk / 'source'
        p.write_text(p.read_text().replace('outside=original', 'outside=user edit'), newline='\n')
        before = p.read_bytes()
        self.git('apply', '--reverse', '--check', str(bootstrap.patches(self.root)[-1]))
        with patch.object(bootstrap, 'NS3_SHA', self.head), self.assertRaises(RuntimeError):
            bootstrap.verify_sdk(self.root, self.sdk)
        self.assertEqual(p.read_bytes(), before)

    def test_wrong_head_is_rejected_without_replacing_checkout(self):
        before = (self.sdk / 'source').read_bytes()
        with self.assertRaisesRegex(ValueError, 'pinned revision'):
            bootstrap.ensure_sdk(self.root)
        self.assertEqual((self.sdk / 'source').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
