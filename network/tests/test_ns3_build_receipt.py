#!/usr/bin/env python3
"""Unit tests for fail-closed ns-3 build receipts (no real build required)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.ns3 import ns3_build_receipt as receipt  # noqa: E402


class ReceiptFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ns3 = root / "ns-3"
        self.project = root / "project/ams-test.cc"
        self.copied = self.ns3 / "scratch/ams-test.cc"
        self.executable = self.ns3 / "build/scratch/ns3.40-ams-test-default"
        self.cache = self.ns3 / "cmake-cache/CMakeCache.txt"
        self.lock = self.ns3 / f".lock-ns3_{sys.platform}_build"
        self.cmake = root / "tools/cmake"
        self.compiler = root / "tools/c++"
        for directory in {
            self.ns3 / "src",
            self.project.parent,
            self.copied.parent,
            self.executable.parent,
            self.cache.parent,
            self.cmake.parent,
        }:
            directory.mkdir(parents=True, exist_ok=True)
        (self.ns3 / "VERSION").write_text("3.40\n", encoding="utf-8")
        (self.ns3 / "src/core.cc").write_text("official core\n", encoding="utf-8")
        self.project.write_text("int main() { return 0; }\n", encoding="utf-8")
        self.copied.write_bytes(self.project.read_bytes())
        self.executable.write_bytes(b"ELF fixture bytes\n")
        self.executable.chmod(0o755)
        self._write_tool(
            self.cmake,
            """#!/bin/sh
printf 'cmake version 3.22.1\\n'
""",
        )
        self._write_tool(
            self.compiler,
            """#!/bin/sh
case "$1" in
  --version) printf 'fixture c++ 11.4.0\\n' ;;
  -dumpmachine) printf 'x86_64-linux-gnu\\n' ;;
  -dumpfullversion) printf '11.4.0\\n' ;;
  *) exit 2 ;;
esac
""",
        )
        self.write_cache()
        self.write_lock()
        self.tree_count, self.tree_hash = receipt.ns3_core_tree_hash(self.ns3)

    @staticmethod
    def _write_tool(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def write_cache(
        self,
        *,
        modules: str = "core,network",
        build_type: str = "default",
    ) -> None:
        self.cache.write_text(
            "\n".join(
                (
                    f"CMAKE_BUILD_TYPE:STRING={build_type}",
                    f"CMAKE_COMMAND:INTERNAL={self.cmake}",
                    f"CMAKE_CXX_COMPILER:FILEPATH={self.compiler}",
                    "CMAKE_CXX_FLAGS:STRING=",
                    "CMAKE_CXX_FLAGS_DEFAULT:STRING=",
                    "CMAKE_EXE_LINKER_FLAGS:STRING=",
                    "CMAKE_GENERATOR:INTERNAL=Unix Makefiles",
                    "CMAKE_MAKE_PROGRAM:FILEPATH=/usr/bin/make",
                    f"NS3_ENABLED_MODULES:STRING={modules}",
                    "NS3_EXAMPLES:BOOL=OFF",
                    f"NS3_SOURCE_DIR:STATIC={self.ns3}",
                    "NS3_TESTS:BOOL=OFF",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def write_lock(
        self,
        *,
        modules: tuple[str, ...] = ("core", "network"),
        build_profile: str = "default",
    ) -> None:
        prefixed = [f"ns3-{item}" for item in modules]
        self.lock.write_text(
            "\n".join(
                (
                    f"launch_dir = {str(self.ns3)!r}",
                    f"run_dir = {str(self.ns3)!r}",
                    f"top_dir = {str(self.ns3)!r}",
                    f"out_dir = {str(self.ns3 / 'build')!r}",
                    f"NS3_ENABLED_MODULES = {prefixed!r}",
                    "NS3_ENABLED_CONTRIBUTED_MODULES = []",
                    "ENABLE_EXAMPLES = False",
                    "ENABLE_TESTS = False",
                    "APPNAME = 'ns'",
                    f"BUILD_PROFILE = {build_profile!r}",
                    "VERSION = '3.40'",
                    f"ns3_runnable_programs = {[str(self.executable)]!r}",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def args(self, mode: str = "create"):
        return receipt.parse_args(
            [
                mode,
                "--ns3-dir",
                str(self.ns3),
                "--program",
                "ams-test",
                "--project-source",
                str(self.project),
                "--copied-source",
                str(self.copied),
                "--executable",
                str(self.executable),
                "--required-modules",
                "core,network",
                "--expected-core-tree-files",
                str(self.tree_count),
                "--expected-core-tree-sha256",
                self.tree_hash,
            ]
        )


class Ns3BuildReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = ReceiptFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_verify_and_copy_are_content_bound_and_read_only(self) -> None:
        created = receipt.create(self.fixture.args())
        verified = receipt.verify(self.fixture.args("verify"))
        self.assertEqual(created, verified)
        self.assertEqual(created.stat().st_mode & 0o222, 0)

        copy = self.fixture.root / "run/metrics/ns3_build_receipt.json"
        args = self.fixture.args("verify")
        args.copy_to = copy
        receipt.verify(args)
        self.assertEqual(copy.read_bytes(), created.read_bytes())
        self.assertEqual(copy.stat().st_mode & 0o222, 0)

        document = json.loads(copy.read_text(encoding="utf-8"))
        subject = document["subject"]
        self.assertEqual(subject["official_source"]["version"], "3.40")
        self.assertEqual(subject["build"]["enabled_modules"], ["core", "network"])
        self.assertEqual(
            subject["build"]["ns3_wrapper_lock"]["enabled_modules"],
            ["core", "network"],
        )
        self.assertEqual(subject["scratch_source"]["project"]["sha256"],
                         subject["scratch_source"]["copied"]["sha256"])
        self.assertGreater(subject["executable"]["size_bytes"], 0)

    def test_repeated_create_reuses_but_never_overwrites_identical_receipt(self) -> None:
        first = receipt.create(self.fixture.args())
        before = first.read_bytes()
        second = receipt.create(self.fixture.args())
        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), before)

        explicit = self.fixture.root / "sentinel.json"
        explicit.write_text('{"sentinel":true}\n', encoding="utf-8")
        args = self.fixture.args()
        args.receipt = explicit
        with self.assertRaises(receipt.ReceiptError):
            receipt.create(args)
        self.assertEqual(explicit.read_text(encoding="utf-8"), '{"sentinel":true}\n')

    def test_copy_is_write_once(self) -> None:
        receipt.create(self.fixture.args())
        copy = self.fixture.root / "run/metrics/ns3_build_receipt.json"
        copy.parent.mkdir(parents=True)
        copy.write_text("sentinel\n", encoding="utf-8")
        args = self.fixture.args("verify")
        args.copy_to = copy
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify(args)
        self.assertEqual(copy.read_text(encoding="utf-8"), "sentinel\n")

    def test_stale_or_replaced_executable_is_rejected(self) -> None:
        receipt.create(self.fixture.args())
        self.fixture.executable.write_bytes(b"different executable\n")
        self.fixture.executable.chmod(0o755)
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify(self.fixture.args("verify"))

    def test_changed_official_source_tree_is_rejected(self) -> None:
        receipt.create(self.fixture.args())
        (self.fixture.ns3 / "src/core.cc").write_text("tampered core\n", encoding="utf-8")
        with self.assertRaisesRegex(receipt.ReceiptError, "core tree hash mismatch"):
            receipt.verify(self.fixture.args("verify"))

    def test_project_and_copied_scratch_must_be_byte_identical(self) -> None:
        self.fixture.copied.write_text("stale copied source\n", encoding="utf-8")
        with self.assertRaisesRegex(receipt.ReceiptError, "not byte-identical"):
            receipt.create(self.fixture.args())

    def test_enabled_modules_must_match_exact_required_set(self) -> None:
        self.fixture.write_cache(modules="core,network,tap-bridge")
        with self.assertRaisesRegex(receipt.ReceiptError, "exact required set"):
            receipt.create(self.fixture.args())

    def test_any_cmake_option_change_invalidates_existing_receipt(self) -> None:
        receipt.create(self.fixture.args())
        self.fixture.write_cache(build_type="release")
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify(self.fixture.args("verify"))

    def test_wrapper_lock_must_match_cache_and_is_content_bound(self) -> None:
        with self.subTest("module mismatch"):
            self.fixture.write_lock(modules=("core",))
            with self.assertRaisesRegex(receipt.ReceiptError, "lock modules differ"):
                receipt.create(self.fixture.args())
        self.fixture.write_lock()
        receipt.create(self.fixture.args())
        with self.subTest("profile mismatch"):
            self.fixture.write_lock(build_profile="release")
            with self.assertRaisesRegex(receipt.ReceiptError, "build profile differs"):
                receipt.verify(self.fixture.args("verify"))

    def test_wrapper_lock_is_parsed_without_executing_code(self) -> None:
        marker = self.fixture.root / "must-not-exist"
        self.fixture.lock.write_text(
            f"__import__('pathlib').Path({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(receipt.ReceiptError, "non-assignment"):
            receipt.create(self.fixture.args())
        self.assertFalse(marker.exists())

    def test_writable_or_malformed_receipt_is_rejected(self) -> None:
        path = receipt.create(self.fixture.args())
        path.chmod(0o644)
        with self.assertRaisesRegex(receipt.ReceiptError, "remains writable"):
            receipt.verify(self.fixture.args("verify"))
        path.chmod(0o444)

    def test_compiler_identity_change_invalidates_receipt(self) -> None:
        receipt.create(self.fixture.args())
        ReceiptFixture._write_tool(
            self.fixture.compiler,
            """#!/bin/sh
case "$1" in
  --version) printf 'fixture c++ 12.0.0\\n' ;;
  -dumpmachine) printf 'x86_64-linux-gnu\\n' ;;
  -dumpfullversion) printf '12.0.0\\n' ;;
  *) exit 2 ;;
esac
""",
        )
        with self.assertRaises(receipt.ReceiptError):
            receipt.verify(self.fixture.args("verify"))

    def test_runtime_scripts_verify_even_when_build_is_skipped(self) -> None:
        m2 = (ROOT_DIR / "network/scripts/run_one_uav_vertical_slice.sh").read_text(
            encoding="utf-8"
        )
        skip_block_end = m2.index("fi\n", m2.index('M2_SKIP_BUILDS'))
        initial_verify = m2.index('python3 "$NS3_RECEIPT_TOOL" verify')
        self.assertGreater(initial_verify, skip_block_end)
        self.assertIn('"metrics/ns3_tap_build_receipt.json",', m2)
        self.assertGreaterEqual(m2.count('python3 "$NS3_RECEIPT_TOOL" verify'), 2)

        core = (ROOT_DIR / "network/ns3/run_ns3_core.sh").read_text(encoding="utf-8")
        self.assertIn('python3 "$RECEIPT_TOOL" verify', core)
        self.assertLess(core.index('python3 "$RECEIPT_TOOL" verify'), core.index('"$NS3_BINARY" --topology='))

        for relative, target in (
            ("network/ns3/build_ns3_core.sh", "scratch/ams-radio-core"),
            ("network/ns3/build_ns3_tap.sh", "scratch/ams-tap-vertical-slice"),
        ):
            build = (ROOT_DIR / relative).read_text(encoding="utf-8")
            self.assertIn('python3 "$RECEIPT_TOOL" create', build)
            self.assertIn("cmake_clean.cmake", build)
            self.assertLess(build.index("cmake_clean.cmake"), build.index(f"./ns3 build {target}"))


if __name__ == "__main__":
    unittest.main()
