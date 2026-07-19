#!/usr/bin/env python3
"""Tests for v2 source/config provenance generation and acceptance checks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.write_run_provenance import (  # noqa: E402
    INHERITED_M0_RECEIPT_MOUNTS,
    build_provenance,
    default_configs_for_profile,
    deterministic_source_hash,
    parse_args,
    ns3_core_tree_hash,
    runtime_manifest_commands,
    runtime_container_identity,
    runtime_capabilities,
    source_files,
    source_files_for_profile,
)
from network.scripts import write_run_provenance as provenance_module  # noqa: E402
from network.validation import evidence as evidence_module  # noqa: E402
from network.validation.evidence import provenance_status  # noqa: E402
from network.validation.qualification_identity import (  # noqa: E402
    BOUNDED_ROOT_IN_RUNTIME_MODE,
    DEFERRED_M0_CAPABILITY_MODE,
)
from network.validation.component_profiles import (  # noqa: E402
    expected_radio_provider_runtime,
)


class ProvenanceV2Tests(unittest.TestCase):
    def test_git_commands_pin_their_exact_cwd_as_safe_directory(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            provenance_module.subprocess, "run", return_value=completed
        ) as runner:
            cwd = Path(temp).resolve(strict=True)
            result = provenance_module.run_command(
                ["git", "rev-parse", "HEAD"], cwd=cwd
            )
        self.assertEqual(result, "ok")
        self.assertEqual(
            runner.call_args.args[0][:3],
            ["git", "-c", f"safe.directory={cwd}"],
        )
        self.assertNotIn("safe.directory=*", runner.call_args.args[0])

    def test_radio_provider_runtime_consumption_is_profile_truthful(self) -> None:
        selected = "tcp_jsonl_real_sionna"
        for profile in (
            "m0",
            "m1_component",
            "flight_capacity_prerequisite",
            "m2_component",
            "m3_component",
        ):
            with self.subTest(profile=profile):
                self.assertEqual(
                    expected_radio_provider_runtime(profile, selected),
                    {
                        "radio_provider_runtime_consumed": False,
                        "runtime_provider_id": "not_applicable_pre_m4",
                        "reason": "profile_pre_m4",
                    },
                )
        for profile in ("m4_capacity_prerequisite", "m4_component"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    expected_radio_provider_runtime(profile, selected),
                    {
                        "radio_provider_runtime_consumed": True,
                        "runtime_provider_id": selected,
                        "reason": "profile_m4_runtime",
                    },
                )
        self.assertEqual(
            expected_radio_provider_runtime("diagnostic", selected),
            {
                "radio_provider_runtime_consumed": True,
                "runtime_provider_id": selected,
                "reason": "diagnostic_full_path",
            },
        )

    def test_m0_source_manifest_ignores_q1_bytes_but_binds_q0_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            q0 = root / "network/q0.py"
            q1 = root / "src/multiagent_simulation/q1.py"
            q0.parent.mkdir(parents=True)
            q1.parent.mkdir(parents=True)
            q0.write_text("q0-v1\n", encoding="utf-8")
            q1.write_text("q1-v1\n", encoding="utf-8")
            vector = {
                "entry_manifest": [
                    {"path": "network/q0.py", "owner": "Q0", "kind": "regular"},
                    {
                        "path": "src/multiagent_simulation/q1.py",
                        "owner": "Q1",
                        "kind": "regular",
                    },
                ]
            }
            m0_files = source_files_for_profile(vector, "m0", root)
            self.assertEqual(m0_files, [q0])
            m0_before = deterministic_source_hash(m0_files, root)

            q1.write_text("q1-v2\n", encoding="utf-8")
            m0_after_q1 = deterministic_source_hash(
                source_files_for_profile(vector, "m0", root), root
            )
            self.assertEqual(m0_before, m0_after_q1)

            q0.write_text("q0-v2\n", encoding="utf-8")
            m0_after_q0 = deterministic_source_hash(
                source_files_for_profile(vector, "m0", root), root
            )
            self.assertNotEqual(m0_before, m0_after_q0)
            self.assertEqual(
                source_files_for_profile(vector, "m1_component", root), [q0, q1]
            )

    def test_inherited_m0_mount_is_exact_per_flight_profile(self) -> None:
        self.assertEqual(
            INHERITED_M0_RECEIPT_MOUNTS,
            {
                "m1_component": "/run/ams/m0-receipt.json",
                "flight_capacity_prerequisite": "/run/ams/prerequisites/m0.json",
            },
        )
        self.assertEqual(
            {profile: str(path) for profile, path in evidence_module.INHERITED_M0_RECEIPT_MOUNTS.items()},
            INHERITED_M0_RECEIPT_MOUNTS,
        )

    def test_profile_default_configs_follow_exact_consumed_prefix(self) -> None:
        entries = [
            {
                "path": "doc/network_radio_integration_plan_v3.md",
                "owner": "Q0",
                "kind": "regular",
            },
            {"path": "network/config/q0.yaml", "owner": "Q0", "kind": "regular"},
            {"path": "network/config/q1.yaml", "owner": "Q1", "kind": "regular"},
            {"path": "network/config/q2.yaml", "owner": "Q2", "kind": "regular"},
            {"path": "network/config/q3.yaml", "owner": "Q3", "kind": "regular"},
            {"path": "network/config/q4.yaml", "owner": "Q4", "kind": "regular"},
            {"path": "network/config/q5.yaml", "owner": "Q5", "kind": "regular"},
            {"path": "network/scripts/not-a-config.py", "owner": "Q0", "kind": "regular"},
        ]
        vector = {"entry_manifest": entries}
        plan = "doc/network_radio_integration_plan_v3.md"
        self.assertEqual(
            default_configs_for_profile(vector, "m0"),
            (plan, "network/config/q0.yaml"),
        )
        self.assertEqual(
            default_configs_for_profile(vector, "m1_component"),
            (plan, "network/config/q0.yaml", "network/config/q1.yaml"),
        )
        self.assertEqual(
            default_configs_for_profile(vector, "m2_component"),
            (
                plan,
                "network/config/q0.yaml",
                "network/config/q1.yaml",
                "network/config/q2.yaml",
            ),
        )
        self.assertEqual(
            default_configs_for_profile(vector, "m3_component")[-1],
            "network/config/q3.yaml",
        )
        self.assertEqual(
            default_configs_for_profile(vector, "m4_component")[-1],
            "network/config/q4.yaml",
        )
        self.assertEqual(
            default_configs_for_profile(vector, "diagnostic")[-1],
            "network/config/q5.yaml",
        )

    def test_source_hash_is_deterministic(self) -> None:
        files = source_files()
        self.assertGreater(len(files), 10)
        first = deterministic_source_hash(files)
        second = deterministic_source_hash(files)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_full_container_id_comes_from_host_bind_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity_file = Path(temp) / "container_id"
            identity_file.write_text("a" * 64 + "\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"AMS_RUNTIME_CONTAINER_ID_FILE": str(identity_file), "HOSTNAME": "b" * 12},
            ):
                identity, source = runtime_container_identity()
        self.assertEqual(identity, "a" * 64)
        self.assertEqual(source, "host_bind_mount")

    def test_ns3_release_tree_uses_relative_path_order(self) -> None:
        ns3 = ROOT_DIR / ".external/ns-3"
        if not ns3.is_dir():
            self.skipTest("ns-3 release tree is not materialized")
        count, digest = ns3_core_tree_hash(ns3)
        self.assertGreater(count, 3000)
        self.assertEqual(
            digest,
            "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8",
        )

    def test_generator_records_current_dirty_state_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "provenance_dirty"
            args = parse_args(
                [
                    "--run-dir",
                    str(run_dir),
                    "--container-image",
                    "multiagent_simulation:test",
                    "--container-digest",
                    "sha256:test",
                ]
            )
            args.config = ["network/config/validation_matrix.yaml"]
            data = build_provenance(args)
            self.assertEqual(data["run_id"], "provenance_dirty")
            self.assertEqual(data["git_dirty"], bool(data["git_status"]))
            self.assertFalse(data["acceptance_eligible"])
            self.assertEqual(len(data["git_diff_sha256"]), 64)
            self.assertIn("runtime_manifests", data["dependency_versions"])
            self.assertIn("pip_freeze", data["dependency_versions"]["runtime_manifests"])
            pip_manifest = data["dependency_versions"]["runtime_manifests"]["pip_freeze"]
            self.assertEqual(pip_manifest["entries"], len(pip_manifest["lines"]))
            self.assertEqual(pip_manifest["lines"], sorted(set(pip_manifest["lines"])))
            self.assertIn("external_sources", data["dependency_versions"])
            self.assertIn("ardupilot_ros2", data["dependency_versions"]["external_sources"])
            self.assertIn("network/config/validation_matrix.yaml", data["config_hashes"])
            source_manifest = data["source_manifest"]
            self.assertIn(".devcontainer/Dockerfile", source_manifest)
            self.assertIn(".devcontainer/ardupilot_ros2_exact.repos", source_manifest)
            self.assertIn(".devcontainer/setup.sh", source_manifest)

    def test_runtime_manifest_commands_exclude_commit_coupled_editables(self) -> None:
        commands = runtime_manifest_commands()
        self.assertEqual(
            commands["pip_freeze"],
            [
                sys.executable,
                "-m",
                "pip",
                "freeze",
                "--all",
                "--exclude-editable",
            ],
        )
        self.assertEqual(
            commands["dpkg"],
            ["dpkg-query", "-W", "-f=${Package}=${Version}\\n"],
        )
        self.assertEqual(commands["ros_packages"], ["ros2", "pkg", "list"])
        with mock.patch.object(
            provenance_module,
            "run_command",
            return_value=(
                "multiagent_simulation_tools==9.0\n"
                "multiagent_simulation==0.0.0\n"
                "PyYAML==6.0.2\n"
            ),
        ):
            manifest = provenance_module.command_manifest(commands["pip_freeze"])
        self.assertEqual(
            manifest["lines"],
            ["PyYAML==6.0.2", "multiagent_simulation_tools==9.0"],
        )
        with mock.patch.object(
            provenance_module,
            "run_command",
            return_value="multiagent_simulation_tools\nmultiagent_simulation\nros_gz_sim\n",
        ):
            manifest = provenance_module.command_manifest(commands["ros_packages"])
        self.assertEqual(
            manifest["lines"], ["multiagent_simulation_tools", "ros_gz_sim"]
        )

    def test_qualification_profile_rejects_missing_or_extra_consumed_nodes(self) -> None:
        cases = (
            ("m0", []),
            ("m0", ["Q0", "Q0"]),
            ("m1_component", ["Q0"]),
            ("m1_component", ["Q0", "Q1", "Q2"]),
            ("flight_capacity_prerequisite", ["Q0"]),
            ("flight_capacity_prerequisite", ["Q0", "Q1", "Q2"]),
            ("m2_component", ["Q0", "Q1"]),
            ("m2_component", ["Q0", "Q1", "Q2", "Q3"]),
            ("m3_component", ["Q0", "Q1", "Q2"]),
            ("m3_component", ["Q0", "Q1", "Q2", "Q3", "Q4"]),
            ("m4_capacity_prerequisite", ["Q0", "Q1", "Q2", "Q3"]),
            (
                "m4_capacity_prerequisite",
                ["Q0", "Q1", "Q2", "Q3", "Q4", "Q5"],
            ),
            ("m4_component", ["Q0", "Q1", "Q2", "Q3"]),
            ("m4_component", ["Q0", "Q1", "Q2", "Q3", "Q4", "Q5"]),
            ("diagnostic", ["Q0"]),
        )
        for profile, nodes in cases:
            with self.subTest(profile=profile, nodes=nodes), tempfile.TemporaryDirectory() as temp:
                arguments = [
                    "--run-dir",
                    str(Path(temp) / "profile_mismatch"),
                    "--qualification-profile",
                    profile,
                ]
                for node in nodes:
                    arguments.extend(["--consumed-node", node])
                args = parse_args(arguments)
                with self.assertRaisesRegex(ValueError, "must consume exactly"):
                    build_provenance(args)

    def test_runtime_capabilities_record_deferred_mode_without_forging_observations(self) -> None:
        observed_commands: list[list[str]] = []

        def unavailable(command: list[str], cwd: Path = ROOT_DIR) -> str | None:
            observed_commands.append(command)
            return None

        security = {
            "uid": 0,
            "gid": 1000,
            "CapPrm": "0000000000203005",
            "CapEff": "0000000000203005",
            "CapBnd": "0000000000203005",
            "NoNewPrivs": 1,
        }
        with mock.patch.object(
            provenance_module, "run_command", side_effect=unavailable
        ), mock.patch.object(
            provenance_module, "_process_security_status", return_value=security
        ), mock.patch.object(
            provenance_module.os, "getuid", return_value=0
        ), mock.patch.dict(
            os.environ,
            {"AMS_M0_CAPABILITY_PROBE_MODE": DEFERRED_M0_CAPABILITY_MODE},
            clear=False,
        ):
            capabilities = runtime_capabilities()
        network = capabilities["network"]
        self.assertEqual(network["qualification_mode"], DEFERRED_M0_CAPABILITY_MODE)
        self.assertIsInstance(network["dev_net_tun"], bool)
        self.assertFalse(network["unshare_network_namespace"])
        self.assertFalse(network["passwordless_sudo"])
        self.assertIn(["/usr/bin/unshare", "-n", "true"], observed_commands)
        self.assertNotIn(["/usr/bin/unshare", "-rn", "true"], observed_commands)
        for key, value in security.items():
            self.assertEqual(network[key], value)

    def test_bounded_root_evidence_reobserves_and_rejects_each_mutated_fact(self) -> None:
        consumption = {
            "profile": "m2_component",
            "consumed_nodes": ["Q0", "Q1", "Q2"],
            "consumed_node_sha256": {
                "Q0": "0" * 64,
                "Q1": "1" * 64,
                "Q2": "2" * 64,
            },
        }
        expected = {
            "uid": 0,
            "gid": 1000,
            "CapPrm": "0000000000203005",
            "CapEff": "0000000000203005",
            "CapBnd": "0000000000203005",
            "NoNewPrivs": 1,
            "dev_net_tun": True,
            "unshare_network_namespace": True,
            "passwordless_sudo": False,
        }
        network = {
            **expected,
            "qualification_mode": BOUNDED_ROOT_IN_RUNTIME_MODE,
        }
        with mock.patch.object(
            evidence_module, "_runtime_security_observation", return_value=expected
        ):
            exact_mode, recorded, independent, policy = (
                evidence_module._bounded_root_runtime_evidence(consumption, network)
            )
        self.assertTrue(exact_mode)
        self.assertTrue(recorded)
        self.assertTrue(independent)
        self.assertEqual(policy, expected)

        mutations = {
            "uid": 1000,
            "gid": 0,
            "CapPrm": "0" * 16,
            "CapEff": "0" * 16,
            "CapBnd": "0" * 16,
            "NoNewPrivs": 0,
            "dev_net_tun": False,
            "unshare_network_namespace": False,
            "passwordless_sudo": True,
        }
        for key, value in mutations.items():
            with self.subTest(key=key), mock.patch.object(
                evidence_module,
                "_runtime_security_observation",
                return_value=expected,
            ):
                mutated = {**network, key: value}
                exact_mode, recorded, independent, _ = (
                    evidence_module._bounded_root_runtime_evidence(
                        consumption, mutated
                    )
                )
                self.assertTrue(exact_mode)
                self.assertFalse(recorded)
                self.assertTrue(independent)

        independently_mutated = {**expected, "CapEff": "0" * 16}
        with mock.patch.object(
            evidence_module,
            "_runtime_security_observation",
            return_value=independently_mutated,
        ):
            _, recorded, independent, _ = (
                evidence_module._bounded_root_runtime_evidence(consumption, network)
            )
        self.assertTrue(recorded)
        self.assertFalse(independent)

    def test_cli_refuses_to_overwrite_existing_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "immutable"
            output = run_dir / "metrics/provenance.json"
            output.parent.mkdir(parents=True)
            output.write_text('{"sentinel": true}\n', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "network/scripts/write_run_provenance.py"),
                    "--run-dir",
                    str(run_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(output.read_text()), {"sentinel": True})
            self.assertIn("already exists", result.stderr)

    def test_generator_fails_closed_when_git_inspection_is_unavailable(self) -> None:
        original = provenance_module.run_command

        def selective_failure(command: list[str], cwd: Path = ROOT_DIR) -> str | None:
            if command[:2] in (["git", "status"], ["git", "diff"]):
                return None
            return original(command, cwd=cwd)

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            provenance_module, "run_command", side_effect=selective_failure
        ):
            args = parse_args(
                [
                    "--run-dir",
                    str(Path(temp) / "git_unavailable"),
                    "--container-image",
                    "multiagent_simulation:latest",
                    "--container-digest",
                    "sha256:" + "a" * 64,
                ]
            )
            args.config = ["network/config/validation_matrix.yaml"]
            data = build_provenance(args)

        self.assertFalse(data["acceptance_eligible"])
        self.assertIn("git status could not be inspected", data["acceptance_blockers"])
        self.assertIn("git diff could not be inspected", data["acceptance_blockers"])
        self.assertNotEqual(data["git_diff_sha256"], hashlib.sha256(b"").hexdigest())

    def test_cross_run_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "expected_run"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "different_run",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {"reference": "image", "digest": "sha256:abc"},
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(json.dumps(record), encoding="utf-8")
            result = provenance_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("run_id", result["proof"])

    def test_structurally_forged_clean_provenance_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "matching_run"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "matching_run",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {"reference": "image", "digest": "sha256:abc"},
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(json.dumps(record), encoding="utf-8")
            result = provenance_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("source", failures)
            self.assertIn("dependency lock", failures)

    def test_missing_qualification_identity_is_rejected_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "missing_qualification"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {},
                "container_image": {},
            }
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            result = provenance_status(run_dir)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "qualification vector is missing or malformed",
            "\n".join(result["details"]["failures"]),
        )

    def test_forged_qualification_vector_reaches_fail_closed_verifier(self) -> None:
        current_commit = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={ROOT_DIR.resolve(strict=True)}",
                "-C",
                str(ROOT_DIR),
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        config_hashes = {
            relative: evidence_module.sha256_file(ROOT_DIR / relative)
            for relative in provenance_module.UNQUALIFIED_CONFIG_FALLBACK
        }
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "forged_qualification"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "generated_utc": "2026-07-16T00:00:00Z",
                "git_commit": current_commit,
                "git_dirty": False,
                "git_status": [],
                "git_diff_sha256": hashlib.sha256(b"").hexdigest(),
                "source_hash": "b" * 64,
                "source_files": 1,
                "source_manifest": {},
                "qualification_content_vector": {
                    "schema_version": 1,
                    "git_commit": current_commit,
                    "vector_sha256": "c" * 64,
                },
                "qualification_consumption": {
                    "profile": "m0",
                    "consumed_nodes": ["Q0"],
                    "consumed_node_sha256": {"Q0": "d" * 64},
                },
                "qualification_checkout": {},
                "plan_contract": {
                    "plan_version": 3,
                    "path": "doc/network_radio_integration_plan_v3.md",
                    "contract_sha256": config_hashes[
                        "doc/network_radio_integration_plan_v3.md"
                    ],
                },
                "config_hashes": config_hashes,
                "dependency_versions": {},
                "container_image": {},
                "implementation": {},
                "dependency_lock_status": "complete",
                "acceptance_blockers": [],
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            result = provenance_status(run_dir)

        self.assertEqual(result["status"], "failed")
        failures = "\n".join(result["details"]["failures"])
        self.assertIn("could not recompute source provenance", failures)
        self.assertIn("qualification", failures.lower())

    def test_unknown_container_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "unknown_container"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "unknown_container",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {"reference": "image", "digest": "unknown"},
                "acceptance_eligible": False,
            }
            (run_dir / "metrics/provenance.json").write_text(json.dumps(record), encoding="utf-8")
            result = provenance_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("not acceptance-eligible", result["proof"])

    def test_nul_in_config_path_is_rejected_without_validator_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "nul_config"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "nul_config",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "git_status": [],
                "git_diff_sha256": "b" * 64,
                "source_hash": "c" * 64,
                "source_manifest": {},
                "config_hashes": {"network/config/bad\u0000path.yaml": "d" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {
                    "reference": "image",
                    "digest": "sha256:" + "e" * 64,
                    "digest_source": "docker_image_inspect_host",
                    "runtime_container_id": "f" * 64,
                },
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps(record), encoding="utf-8"
            )

            result = provenance_status(run_dir)

            self.assertEqual(result["status"], "failed")
            self.assertIn("config hash path is invalid", "\n".join(result["details"]["failures"]))

    def test_malformed_dependency_lock_mapping_fails_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake_root = Path(temp)
            lock_path = fake_root / "network/config/dependency_lock.yaml"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("[]\n", encoding="utf-8")
            run_dir = fake_root / "malformed_lock"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "generated_utc": "2026-07-12T12:00:00Z",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "git_status": [],
                "git_diff_sha256": hashlib.sha256(b"").hexdigest(),
                "source_hash": "b" * 64,
                "source_files": 1,
                "source_manifest": {"source": "c" * 64},
                "config_hashes": {"config": "d" * 64},
                "dependency_versions": {},
                "container_image": {},
                "dependency_lock_status": "complete",
                "acceptance_blockers": [],
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            with mock.patch.object(evidence_module, "ROOT_DIR", fake_root):
                result = provenance_status(run_dir)

            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "dependency lock root is not a mapping",
                "\n".join(result["details"]["failures"]),
            )

    def test_generator_marks_malformed_dependency_lock_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            provenance_module.yaml, "safe_load", return_value=[]
        ):
            args = parse_args(
                [
                    "--run-dir",
                    str(Path(temp) / "malformed_generator_lock"),
                ]
            )
            args.config = ["network/config/validation_matrix.yaml"]
            data = build_provenance(args)

        self.assertFalse(data["acceptance_eligible"])
        self.assertIn(
            "dependency lock root is not a mapping", data["acceptance_blockers"]
        )


if __name__ == "__main__":
    unittest.main()
