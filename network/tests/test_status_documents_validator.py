#!/usr/bin/env python3
"""Adversarial tests for the post-receipt live status lint."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Callable


from network.validation.qualification_identity import qualification_content_vector
from network.scripts.validate_m0_baseline import EXPECTED_DEPENDENCY_RECORDS
from network.scripts.validate_status_documents import (
    STATUS_PATHS,
    M1_NEXT_COMMAND_ARGV,
    M2_BLOCKING_PREREQUISITES,
    _container_immutable_fingerprint,
    _derive_execution_contract,
    _git_blob_record,
    _m1_portable_manifest,
    _portable_manifest_from_snapshot,
    _validate_fresh_raw,
    canonical_status_metadata_block,
    main,
    validate_live_status,
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class LiveStatusFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ams-status-lint-")
        self.root = Path(self.temporary.name)
        self.run_id = "m0_v3_baseline_20990101T000000Z"
        self.container_id = "1" * 64
        self.image_digest = "sha256:" + "2" * 64
        self._write_initial_tree()
        self._git("init", "-q")
        self._git("config", "user.email", "status-lint@example.invalid")
        self._git("config", "user.name", "Status Lint Test")
        self._git("add", ".")
        self._git("commit", "-qm", "technical base")
        self.base = self._git("rev-parse", "HEAD").stdout.strip()
        self.vector = qualification_content_vector(self.root, self.base)
        self.execution_contract = _derive_execution_contract(
            self.root, self.base, self.vector
        )
        execution_policy = self.execution_contract["m0_execution_policy"]
        vector_entries = {
            entry["path"]: entry for entry in self.vector["entry_manifest"]
        }
        self.host_execution_identity = {
            "schema_version": 1,
            "contract": "ams.m0.host-execution-identity/v1",
            "execution_policy_sha256": self.execution_contract[
                "m0_execution_policy_sha256"
            ],
            "host_path": execution_policy["host_final_path"],
            "host_executables": {
                path: {"bytes": 1, "sha256": digest}
                for path, digest in execution_policy[
                    "host_final_executable_sha256"
                ].items()
            },
            "host_python": {
                "sys_path": execution_policy["host_final_python_sys_path"],
                "third_party_imports": execution_policy[
                    "host_final_python_imports"
                ],
            },
            "source_executables": {
                path: {
                    "git_mode": vector_entries[path]["git_mode"],
                    "sha256": vector_entries[path]["blob_sha256"],
                }
                for path in execution_policy["critical_source_executables"]
            },
        }
        self.plan_sha256 = sha256(
            (self.root / "doc/network_radio_integration_plan_v3.md").read_bytes()
        )
        self.receipt = self._make_receipt()
        self.receipt_path = (
            self.root / f"runs/{self.run_id}/metrics/m0_host_final_receipt.json"
        )
        self._write_receipt()
        self.metadata = self._make_metadata()
        self._write_status_documents()
        self._git("add", *STATUS_PATHS)
        self._git("commit", "-qm", "status only")
        self._set_run_directory_modes(0o500)

    def close(self) -> None:
        self._set_run_directory_modes(0o700)
        self.temporary.cleanup()

    def _set_run_directory_modes(self, mode: int) -> None:
        run_root = self.root / f"runs/{self.run_id}"
        if not run_root.exists():
            return
        for path in run_root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(mode)
        run_root.chmod(mode)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def _write(self, relative: str, content: str, mode: int = 0o644) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)

    def _write_initial_tree(self) -> None:
        self._write(".gitignore", "runs/\n")
        self._write(
            "doc/network_radio_integration_plan_v3.md",
            "# Fixture v3 contract\n\nOnly exact post-receipt status descendants count.\n",
        )
        policy = {
            "schema_version": 2,
            "contract": "q0_q1_q2_granular/v1",
            "policy_id": "q0_q1_q2_granular/v1",
            "mutable_status_exclusions": sorted(STATUS_PATHS),
            "selective_descendant_reuse_allowed": True,
            "profile_consumption": {
                "diagnostic": [],
                "m0": ["Q0"],
                "m1_component": ["Q0", "Q1"],
                "flight_capacity_prerequisite": ["Q0", "Q1"],
                "m2_component": ["Q0", "Q1", "Q2"],
                "m3_component": ["Q0", "Q1", "Q2", "Q3"],
                "m4_capacity_prerequisite": ["Q0", "Q1", "Q2", "Q3", "Q4"],
                "m4_component": ["Q0", "Q1", "Q2", "Q3", "Q4"],
            },
            "default_owner": "Q0",
            "explicit_owners": {f"Q{index}": [] for index in range(9)},
        }
        self._write(
            "network/config/qualification_path_ownership.json",
            json.dumps(policy, indent=2, sort_keys=True) + "\n",
        )
        manifest = {
            "schema_version": 1,
            "contract": "ams.qualification-test-manifest/v1",
            "node": "Q0",
            "discovery": {
                "start_directory": "network/tests",
                "pattern": "test_*.py",
            },
            "test_modules": ["test_fixture"],
            "ordered_test_ids": ["test_fixture.StatusFixtureTests.test_pass"],
        }
        manifest_payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        self._write(
            "network/config/m0_test_manifest.json", manifest_payload.decode("utf-8")
        )
        self._write(
            "network/config/dependency_lock.yaml",
            "schema_version: 2\n"
            "m0_test_manifest:\n"
            "  path: network/config/m0_test_manifest.json\n"
            f"  sha256: {sha256(manifest_payload)}\n"
            "  ordered_test_count: 1\n"
            "m0_execution_policy:\n"
            "  schema_version: 1\n"
            "  host_final_path: /usr/bin:/bin\n"
            "  host_final_executable_sha256:\n"
            f"    /usr/bin/fake: {'a' * 64}\n"
            "  host_final_python_sys_path:\n"
            "    - <repository_root>\n"
            "  host_final_python_imports:\n"
            "    fixture.module:\n"
            "      path: /usr/lib/python3/fixture.py\n"
            "      bytes: 1\n"
            f"      sha256: {'c' * 64}\n"
            "  critical_source_executables:\n"
            "    - scripts/run_acceptance_container.sh\n"
            "    - network/scripts/run_five_uav_health.sh\n"
            "m0_python_import_policy:\n"
            "  schema_version: 1\n"
            "m1_runtime_identity:\n"
            f"  container_image_digest: {self.image_digest}\n"
            "dependencies:\n"
            "  ros:\n"
            "    project_image_reference: multiagent_simulation:latest\n"
            f"    project_image_digest: {self.image_digest}\n",
        )
        initial_rows = "".join(
            f"| M{index} | `{state}` | fixture |\n"
            for index, state in enumerate(
                ["in_progress", *("not_started" for _ in range(8))]
            )
        )
        self._write(
            "network/PROGRESS.md",
            initial_rows
            + "Fully closed sequential milestones: **0**\n"
            + "Customer-ready: **false**\nActive milestone: **M0**\n",
        )
        self._write(
            "network/VALIDATION_REPORT.md",
            initial_rows
            + "Fully closed milestones: **0**\nCustomer-ready: **false**\n",
        )
        self._write(
            "network/NEXT_TASK.md",
            "Fully closed milestones: **0**\nCustomer-ready: **false**\n"
            "Active milestone:\n**M0**\n",
        )
        self._write(
            "scripts/run_acceptance_container.sh", "#!/bin/sh\nexec \"$@\"\n", 0o755
        )
        self._write(
            "network/scripts/run_five_uav_health.sh", "#!/bin/sh\nexit 0\n", 0o755
        )

    @staticmethod
    def _snapshot(tag: str) -> dict[str, Any]:
        entries = {
            "logs": {
                "kind": "directory",
                "mode": 0o500,
                "device": 1,
                "inode": 2,
                "links": 2,
                "mtime_ns": 3,
                "ctime_ns": 4,
            },
            "metrics": {
                "kind": "directory",
                "mode": 0o500,
                "device": 1,
                "inode": 4,
                "links": 2,
                "mtime_ns": 3,
                "ctime_ns": 4,
            },
            f"logs/{tag}.log": {
                "kind": "file",
                "mode": 0o400,
                "device": 1,
                "inode": 3,
                "links": 1,
                "mtime_ns": 3,
                "ctime_ns": 4,
                "bytes": 1,
                "sha256": sha256(b"x"),
            }
        }
        return {
            "root_identity": {
                "device": 1,
                "inode": 1,
                "mode": 0o500,
                "mtime_ns": 3,
                "ctime_ns": 4,
            },
            "entries": entries,
            "entry_count": len(entries),
            "total_file_bytes": 1,
            "tree_sha256": sha256(canonical(entries)),
        }

    def _write_control_files(self) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
        control_dir = self.root / f"runs/{self.run_id}/host_validation"
        control_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = f"/tmp/.ams-m0-artifacts-{self.run_id}.Abcdef1234"
        source_path = "/tmp/ams-m0-source.Abcdef1234"
        identity_path = "/tmp/ams-container-id.Abcdef1234"
        environment = [
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            "ROS_DISTRO=humble",
            "DEBIAN_FRONTEND=noninteractive",
            "GZ_VERSION=harmonic",
            "USER=ubuntu",
            "LOGNAME=ubuntu",
            "HOME=/home/ubuntu",
            "NVIDIA_VISIBLE_DEVICES=all",
            "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            "SIONNA_MITSUBA_VARIANT=cuda_ad_mono_polarized",
            "AMS_CONTAINER_IMAGE=multiagent_simulation:latest",
            f"AMS_CONTAINER_IMAGE_DIGEST={self.image_digest}",
            "AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host",
            "AMS_RUNTIME_CONTAINER_ID_FILE=/run/ams/container_id",
            "AMS_M0_SOURCE_MODE=clean_git_clone_ro",
            f"AMS_M0_SOURCE_COMMIT={self.base}",
            "AMS_M0_PROJECT_OVERLAY_MODE=none_q0_source_only",
            "AMS_M0_ARTIFACT_ROOT=/run/ams/m0-artifacts",
            "AMS_M0_COLLECTION_SECURITY=cap_drop_all_no_new_privileges",
            "AMS_M0_CAPABILITY_PROBE_MODE=host_final_isolated_exact_image",
        ]
        config = {
            "Image": self.image_digest,
            "User": "ubuntu",
            "Entrypoint": ["/ros_entrypoint.sh"],
            "Cmd": [
                "scripts/acceptance_entrypoint.sh",
                "env",
                f"RUN_ID={self.run_id}",
                "network/scripts/run_m0_baseline.sh",
            ],
            "WorkingDir": "/workspace/multiagent_simulation",
            "Env": environment,
        }
        host_config = {
            "Privileged": False,
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"},
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Devices": [],
            "DeviceRequests": [
                {
                    "Driver": "",
                    "Count": -1,
                    "DeviceIDs": None,
                    "Capabilities": [["compute", "utility", "gpu"]],
                    "Options": {},
                }
            ],
        }
        mounts = [
            {
                "Type": "bind",
                "Source": identity_path,
                "Destination": "/run/ams/container_id",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": source_path,
                "Destination": "/workspace/multiagent_simulation",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": artifact_path,
                "Destination": "/run/ams/m0-artifacts",
                "Mode": "rw",
                "RW": True,
                "Propagation": "rprivate",
            },
            {
                "Type": "bind",
                "Source": str(self.root / ".external/ns-3"),
                "Destination": "/workspace/multiagent_simulation/.external/ns-3",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            },
        ]
        common = {
            "Id": self.container_id,
            "Image": self.image_digest,
            "RestartCount": 0,
            "Config": config,
            "HostConfig": host_config,
            "Mounts": mounts,
        }
        initial_container = json.dumps(
            [
                {
                    **common,
                    "State": {"Status": "created", "Running": False},
                }
            ],
            sort_keys=True,
        ).encode("utf-8")
        final_container = json.dumps(
            [
                {
                    **common,
                    "State": {
                        "Status": "exited",
                        "Running": False,
                        "Paused": False,
                        "Restarting": False,
                        "OOMKilled": False,
                        "Dead": False,
                        "ExitCode": 0,
                    },
                }
            ],
            sort_keys=True,
        ).encode("utf-8")
        image = json.dumps([{"Id": self.image_digest}], sort_keys=True).encode("utf-8")
        payloads = {
            "retained/initial_container_inspect.json": initial_container,
            "retained/initial_image_inspect.json": image,
            "retained/final_container_inspect.json": final_container,
            "retained/final_image_inspect.json": image,
        }
        prestart = {
            "schema_version": 1,
            "contract": "ams.m0.prestart-inspection/v1",
            "created_utc": "2099-01-01T00:00:00Z",
            "container_id": self.container_id,
            "image_id": self.image_digest,
            "artifact_root_initial": {
                "path": artifact_path,
                "device": 1,
                "inode": 2,
                "mode": 0o700,
                "entry_count": 0,
                "content_manifest_sha256": sha256(b"[]"),
            },
            "initial_container_inspect": {
                "path": "initial_container_inspect.json",
                "bytes": len(initial_container),
                "sha256": sha256(initial_container),
            },
            "initial_image_inspect": {
                "path": "initial_image_inspect.json",
                "bytes": len(image),
                "sha256": sha256(image),
            },
        }
        payloads["retained/prestart_inspection_record.json"] = (
            json.dumps(prestart, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        payloads.update(
            {
                "fresh/initial_container_inspect.json": b"fresh-initial\n",
                "fresh/final_container_inspect.json": b"fresh-final\n",
                "fresh/image_inspect.json": b"fresh-image\n",
                "fresh/container_stdout.txt": b"",
                "fresh/container_stderr.txt": b"",
                "fresh/operational_snapshot_before.json": b"{}\n",
                "fresh/operational_snapshot_after.json": b"{}\n",
                "capability/initial_container_inspect.json": b"cap-initial\n",
                "capability/final_container_inspect.json": b"cap-final\n",
                "capability/image_inspect.json": b"cap-image\n",
                "capability/stdout.txt": b"capability_probe_passed\n",
                "capability/stderr.txt": b"",
                "capability/command.json": b"{}\n",
                "source/identity.json": b"{}\n",
                "execution/host_identity.json": (
                    json.dumps(
                        self.host_execution_identity, indent=2, sort_keys=True
                    )
                    + "\n"
                ).encode("utf-8"),
            }
        )
        for name in (
            "check_deps.stdout",
            "check_deps.stderr",
            "check_deps.exit_code",
            "runtime_lock.json",
            "runtime_lock.stderr",
            "runtime_lock.exit_code",
            "python_guard.json",
            "suite_runner.stdout",
            "suite_runner.stderr",
            "suite_runner.exit_code",
            f"{self.run_id}/logs/m0_validation_suite.log",
            f"{self.run_id}/metrics/m0_validation_suite.json",
        ):
            payloads[f"fresh/output/{name}"] = (name + "\n").encode("utf-8")
        records: dict[str, dict[str, Any]] = {}
        for name, payload in payloads.items():
            path = control_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o444)
            records[name] = {
                "bytes": len(payload),
                "sha256": sha256(payload),
                "published_mode": 0o400,
            }
        embedded = {
            "schema_version": 1,
            "contract": "ams.m0.host-validation-content/v1",
            "files": dict(sorted(records.items())),
            "file_count": len(records),
            "content_sha256": sha256(canonical(dict(sorted(records.items())))),
        }
        embedded_payload = (
            json.dumps(embedded, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        embedded_path = control_dir / "content_manifest.json"
        embedded_path.write_bytes(embedded_payload)
        embedded_path.chmod(0o444)
        payloads["content_manifest.json"] = embedded_payload
        records["content_manifest.json"] = {
            "bytes": len(embedded_payload),
            "sha256": sha256(embedded_payload),
            "published_mode": 0o400,
        }
        return records, payloads

    def _make_receipt(self) -> dict[str, Any]:
        captured_log = self.root / f"runs/{self.run_id}/logs/captured.log"
        captured_log.parent.mkdir(parents=True, exist_ok=True)
        captured_log.write_bytes(b"x")
        captured_log.chmod(0o444)
        (self.root / f"runs/{self.run_id}/metrics").mkdir(parents=True, exist_ok=True)
        records, payloads = self._write_control_files()
        execution_contract = self.execution_contract
        source = {
            "git_commit": self.base,
            "source_file_count": execution_contract["source_file_count"],
            "source_binding_sha256": execution_contract["source_binding_sha256"],
            "qualification_content_vector": self.vector,
            "frozen_test_manifest_sha256": execution_contract[
                "frozen_test_manifest_sha256"
            ],
            "frozen_test_count": execution_contract["frozen_test_count"],
            "plan_path": "doc/network_radio_integration_plan_v3.md",
            "plan_sha256": self.plan_sha256,
        }
        artifact = self._snapshot("captured")
        retained = {
            "container_id": self.container_id,
            "image_digest": self.image_digest,
            "source_snapshot": "/tmp/ams-m0-source.Abcdef1234",
            "prestart_record_sha256": sha256(
                payloads["retained/prestart_inspection_record.json"]
            ),
            "initial_container_inspect_sha256": sha256(
                payloads["retained/initial_container_inspect.json"]
            ),
            "initial_image_inspect_sha256": sha256(
                payloads["retained/initial_image_inspect.json"]
            ),
            "final_container_inspect_sha256": sha256(
                payloads["retained/final_container_inspect.json"]
            ),
            "final_image_inspect_sha256": sha256(
                payloads["retained/final_image_inspect.json"]
            ),
            "immutable_fingerprint_sha256": sha256(
                canonical(
                    _container_immutable_fingerprint(
                        json.loads(payloads["retained/final_container_inspect.json"])[0]
                    )
                )
            ),
            "mount_sources": sorted(
                [
                    f"/tmp/.ams-m0-artifacts-{self.run_id}.Abcdef1234",
                    "/tmp/ams-container-id.Abcdef1234",
                    "/tmp/ams-m0-source.Abcdef1234",
                    str(self.root / ".external/ns-3"),
                ]
            ),
            "raw_sha256": {
                name: sha256(payloads[name])
                for name in (
                    "retained/prestart_inspection_record.json",
                    "retained/initial_container_inspect.json",
                    "retained/initial_image_inspect.json",
                    "retained/final_container_inspect.json",
                    "retained/final_image_inspect.json",
                )
            },
        }
        fresh = {
            "container_id": "6" * 64,
            "image_digest": self.image_digest,
            "exit_code": 0,
            "dependency_record_count": len(EXPECTED_DEPENDENCY_RECORDS),
            "dependency_warning_count": 0,
            "dependency_stdout_sha256": "7" * 64,
            "runtime_lock_sha256": "8" * 64,
            "passing_test_count": execution_contract["frozen_test_count"],
            "unittest_stderr_sha256": "9" * 64,
            "python_import_trace": {
                "schema_version": 1,
                "contract": "ams.m0.python-import-trace/v1",
            },
            "python_guard": {
                "guard_marker": True,
                "no_site": 0,
                "sitecustomize_path": "/workspace/multiagent_simulation/network/scripts/m0_python_guard/sitecustomize.py",
                "usercustomize_loaded": False,
            },
            "artifact_snapshot_before": self._snapshot("fresh"),
            "artifact_snapshot_after": self._snapshot("fresh"),
            "prestart_container_inspect_sha256": "a" * 64,
            "final_container_inspect_sha256": "b" * 64,
            "container_stdout_sha256": "c" * 64,
            "container_stderr_sha256": "d" * 64,
            "raw_sha256": {
                name: sha256(payloads[name])
                for name in (
                    "fresh/initial_container_inspect.json",
                    "fresh/final_container_inspect.json",
                    "fresh/image_inspect.json",
                    "fresh/container_stdout.txt",
                    "fresh/container_stderr.txt",
                    "fresh/operational_snapshot_before.json",
                    "fresh/operational_snapshot_after.json",
                )
            },
        }
        fresh["python_import_trace_sha256"] = sha256(
            canonical(fresh["python_import_trace"])
        )
        capability = {
            "contract": "ams.m0.isolated-capability-probe/v1",
            "container_id": "5" * 64,
            "image_digest": self.image_digest,
            "exit_code": 0,
            "no_candidate_mounts": True,
            "tun_device": True,
            "passwordless_sudo": True,
            "unshare_network_namespace": True,
            "raw_sha256": {
                name: sha256(payloads[name])
                for name in (
                    "capability/initial_container_inspect.json",
                    "capability/final_container_inspect.json",
                    "capability/image_inspect.json",
                    "capability/stdout.txt",
                    "capability/stderr.txt",
                    "capability/command.json",
                )
            },
        }
        host_content_manifest = {
            "schema_version": 1,
            "contract": "ams.m0.host-validation-content/v1",
            "files": dict(sorted(records.items())),
            "file_count": len(records),
            "content_sha256": sha256(canonical(dict(sorted(records.items())))),
        }
        host_details = {
            "failures": [],
            "expected_container_id": self.container_id,
            "image_digest": self.image_digest,
            "source_before": source,
            "source_after": source,
            "artifact_before": artifact,
            "artifact_after": artifact,
            "artifact_content_manifest": _portable_manifest_from_snapshot(artifact),
            "rederived_captured_gates": None,
            "external_before": {},
            "external_after": {},
            "producer_source_identity": source,
            "fresh_source_before": source,
            "fresh_source_after": source,
            "retained_container_initial_final": retained,
            "retained_container_reinspection": retained,
            "fresh_exact_image_reexecution": fresh,
            "isolated_target_runtime_capability": capability,
            "host_validation_content_manifest": host_content_manifest,
            "host_execution_identity": self.host_execution_identity,
        }
        gates = {
            "runtime_lock": {
                "status": "passed",
                "proof": "runtime lock passed",
                "details": {"failures": [], "exit_code": 0},
            },
            "dependency_check": {
                "status": "passed",
                "proof": "dependency check passed",
                "details": {
                    "exit_code": 0,
                    "raw_log_sha256": "e" * 64,
                    "observed_record_count": len(EXPECTED_DEPENDENCY_RECORDS),
                    "observed_records": [
                        {"label": label, "status": "PASS"}
                        for label in EXPECTED_DEPENDENCY_RECORDS
                    ],
                    "warning_count": 0,
                },
            },
            "validation_adversarial_suite": {
                "status": "passed",
                "proof": "suite passed",
                "details": {
                    "failures": [],
                    "expected_test_count": execution_contract["frozen_test_count"],
                    "raw_log_sha256": "f" * 64,
                    "raw_passing_test_count": execution_contract["frozen_test_count"],
                    "required_coverage": {"fixture": ["fixture.test"]},
                },
            },
            "provenance": {
                "status": "passed",
                "proof": "provenance passed",
                "details": {
                    "provenance_status": {
                        "status": "passed",
                        "proof": "fixture provenance passed",
                    }
                },
            },
        }
        host_details["rederived_captured_gates"] = dict(gates)
        gates["host_final"] = {
            "status": "passed",
            "proof": "host-final passed",
            "details": host_details,
        }
        contract_payload = {
            "run_id": self.run_id,
            "source": source,
            "image_digest": self.image_digest,
            "artifact_content_sha256": host_details["artifact_content_manifest"][
                "content_sha256"
            ],
            "host_validation_content_sha256": host_content_manifest["content_sha256"],
            "isolated_capability_contract": capability["contract"],
            "host_execution_identity_sha256": sha256(
                canonical(host_details["host_execution_identity"])
            ),
            "consumed_nodes": ["Q0"],
        }
        return {
            "schema_version": 3,
            "contract": "ams.m0.host-final-receipt/v1",
            "probe": "m0_dependency_provenance",
            "milestone": "M0",
            "run_id": self.run_id,
            "run_dir": f"runs/{self.run_id}",
            "published_run_dir": f"runs/{self.run_id}",
            "receipt_path": f"runs/{self.run_id}/metrics/m0_host_final_receipt.json",
            "scope": {
                "dependency_check": True,
                "runtime_lock": True,
                "validation_adversarial_suite": True,
                "provenance": True,
                "host_final": True,
                "packet_path": False,
                "sealing": False,
                "attestation": False,
            },
            "p0_eligible": False,
            "captured_qualified": True,
            "formal_accepted": True,
            "passed": True,
            "consumed_nodes": ["Q0"],
            "qualification_content_vector": self.vector,
            "plan_contract": {
                "plan_version": 3,
                "path": "doc/network_radio_integration_plan_v3.md",
                "contract_sha256": self.plan_sha256,
            },
            "qualification_contract_sha256": sha256(canonical(contract_payload)),
            "failures": [],
            "gates": gates,
            "host_validation_raw": {
                "path": f"runs/{self.run_id}/host_validation",
                "never_mounted": True,
                "contract": host_content_manifest["contract"],
                "file_count": host_content_manifest["file_count"],
                "content_sha256": host_content_manifest["content_sha256"],
                "files": records,
            },
        }

    def _write_receipt(self) -> None:
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        self.receipt_path.chmod(0o600) if self.receipt_path.exists() else None
        self.receipt_path.write_text(
            json.dumps(self.receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.receipt_path.chmod(0o444)

    def _make_metadata(self) -> dict[str, Any]:
        argv = list(M1_NEXT_COMMAND_ARGV)
        return {
            "schema_version": 1,
            "contract": "ams.live-status/v1",
            "plan_contract": {
                "path": "doc/network_radio_integration_plan_v3.md",
                "sha256": self.plan_sha256,
            },
            "technical_base_commit": self.base,
            "execution_commit": self.base,
            "evidence": {
                "kind": "m0_host_final_receipt",
                "milestone": "M0",
                "run_id": self.run_id,
                "receipt_path": f"runs/{self.run_id}/metrics/m0_host_final_receipt.json",
                "receipt_sha256": sha256(self.receipt_path.read_bytes()),
                "qualification_contract_sha256": self.receipt[
                    "qualification_contract_sha256"
                ],
            },
            "qualification": {
                "policy_id": self.vector["policy_id"],
                "policy_path": "network/config/qualification_path_ownership.json",
                "policy_sha256": self.vector["policy_sha256"],
                "vector_commit": self.vector["git_commit"],
                "vector_sha256": self.vector["vector_sha256"],
                "consumed_nodes": ["Q0"],
            },
            "state": {
                "active_milestone": "M1",
                "customer_ready": False,
                "fully_closed_sequential_milestones": 1,
            },
            "next_command": {
                "milestone": "M1",
                "argv": argv,
                "argv_sha256": sha256(canonical(argv)),
                "tracked_inputs": [
                    _git_blob_record(self.root, self.base, path)
                    for path in (
                        "network/scripts/run_five_uav_health.sh",
                        "scripts/run_acceptance_container.sh",
                    )
                ],
            },
        }

    def _write_status_documents(self) -> None:
        rows = "".join(
            f"| M{index} | `{state}` | fixture |\n"
            for index, state in enumerate(
                ["passed", "in_progress", *("not_started" for _ in range(7))]
            )
        )
        block = canonical_status_metadata_block(self.metadata)
        self._write(
            "network/PROGRESS.md",
            rows
            + "Fully closed sequential milestones: **1**\n"
            + "Customer-ready: **false**\nActive milestone: **M1**\n\n"
            + block
            + "\n",
        )
        self._write(
            "network/VALIDATION_REPORT.md",
            rows
            + "Fully closed milestones: **1**\nCustomer-ready: **false**\n\n"
            + block
            + "\n",
        )
        self._write(
            "network/NEXT_TASK.md",
            "Fully closed sequential milestones: **1**\nCustomer-ready: **false**\n"
            "Active milestone:\n**M1**\n\n"
            + block
            + "\n",
        )

    def rewrite_receipt(self, mutation: Callable[[dict[str, Any]], None]) -> None:
        mutation(self.receipt)
        self._write_receipt()
        self.metadata["evidence"]["receipt_sha256"] = sha256(self.receipt_path.read_bytes())
        self.metadata["evidence"]["qualification_contract_sha256"] = self.receipt.get(
            "qualification_contract_sha256"
        )
        self._write_status_documents()
        self._git("add", *STATUS_PATHS)
        self._git("commit", "-qm", "cite changed receipt")

    def refresh_qualification_contract_hash(self) -> None:
        details = self.receipt["gates"]["host_final"]["details"]
        payload = {
            "run_id": self.run_id,
            "source": details["source_after"],
            "image_digest": details["image_digest"],
            "artifact_content_sha256": details["artifact_content_manifest"][
                "content_sha256"
            ],
            "host_validation_content_sha256": details[
                "host_validation_content_manifest"
            ]["content_sha256"],
            "isolated_capability_contract": details[
                "isolated_target_runtime_capability"
            ]["contract"],
            "host_execution_identity_sha256": sha256(
                canonical(details["host_execution_identity"])
            ),
            "consumed_nodes": ["Q0"],
        }
        self.receipt["qualification_contract_sha256"] = sha256(canonical(payload))

    def rewrite_metadata(self) -> None:
        self._write_status_documents()
        self._git("add", *STATUS_PATHS)
        self._git("commit", "-qm", "update status metadata")


class StatusDocumentsLiveValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sys_path_before = list(sys.path)
        self.addCleanup(self.assert_sys_path_unchanged)
        self.fixture = LiveStatusFixture()
        self.rederived_gates = copy.deepcopy(
            {
                name: record
                for name, record in self.fixture.receipt["gates"].items()
                if name != "host_final"
            }
        )
        self.rederive_patch = mock.patch(
            "network.scripts.validate_status_documents._rederive_published_m0",
            return_value=(self.rederived_gates, None),
        )
        self.rederive_patch.start()
        self.raw_semantic_patches = [
            mock.patch(
                f"network.scripts.validate_status_documents.{name}",
                return_value=[],
            )
            for name in (
                "_validate_source_raw",
                "_validate_fresh_raw",
                "_validate_capability_raw",
            )
        ]
        for patcher in self.raw_semantic_patches:
            patcher.start()

    def assert_sys_path_unchanged(self) -> None:
        observed = list(sys.path)
        sys.path[:] = self.sys_path_before
        self.assertEqual(observed, self.sys_path_before)

    def tearDown(self) -> None:
        for patcher in reversed(self.raw_semantic_patches):
            patcher.stop()
        self.rederive_patch.stop()
        self.fixture.close()

    def test_exact_status_only_descendant_and_host_receipt_pass(self) -> None:
        result = validate_live_status(self.fixture.root)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["report_commit"], self.fixture._git("rev-parse", "HEAD").stdout.strip())

        initial = {
            "Mounts": [
                {"Destination": "/workspace", "Source": "/tmp/source"},
                {"Destination": "/run/output", "Source": "/tmp/output"},
            ]
        }
        final = copy.deepcopy(initial)
        final["Mounts"].reverse()
        self.assertEqual(
            _container_immutable_fingerprint(initial),
            _container_immutable_fingerprint(final),
        )

    def test_passing_booleans_cannot_hide_forged_host_reexecution(self) -> None:
        self.fixture.rewrite_receipt(
            lambda receipt: receipt["gates"]["host_final"]["details"].update(
                {"fresh_exact_image_reexecution": {}}
            )
        )
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("fresh exact-image" in item for item in result["failures"]), result)

    def test_captured_gate_must_match_independent_published_revalidation(self) -> None:
        self.rederived_gates["runtime_lock"] = {
            "status": "failed",
            "proof": "fixture raw runtime lock failed",
            "details": {"failures": ["fixture failure"], "exit_code": 1},
        }
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any("captured gates differ" in item for item in result["failures"]),
            result,
        )

    def test_qualification_contract_hash_is_independently_rederived(self) -> None:
        self.fixture.rewrite_receipt(
            lambda receipt: receipt.update({"qualification_contract_sha256": "f" * 64})
        )
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any("qualification contract hash does not rederive" in item for item in result["failures"]),
            result,
        )

    def test_locked_image_identity_is_rederived_from_execution_commit(self) -> None:
        def mutate(receipt: dict[str, Any]) -> None:
            receipt["gates"]["host_final"]["details"]["image_digest"] = (
                "sha256:" + "e" * 64
            )
            self.fixture.refresh_qualification_contract_hash()

        self.fixture.rewrite_receipt(mutate)
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("runtime identities" in item for item in result["failures"]), result)

    def test_host_executable_identity_is_rederived_from_locked_policy(self) -> None:
        def mutate(receipt: dict[str, Any]) -> None:
            identity = receipt["gates"]["host_final"]["details"][
                "host_execution_identity"
            ]
            identity["host_executables"]["/usr/bin/fake"]["sha256"] = "b" * 64
            self.fixture.refresh_qualification_contract_hash()

        self.fixture.rewrite_receipt(mutate)
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(
            any(
                "host-final executable identity is invalid" in item
                for item in result["failures"]
            ),
            result,
        )

    def test_explicitly_failing_fresh_runtime_lock_raw_is_rejected(self) -> None:
        control = (
            self.fixture.root
            / f"runs/{self.fixture.run_id}/host_validation"
        )
        payloads = {
            path.relative_to(control).as_posix(): path.read_bytes()
            for path in control.rglob("*")
            if path.is_file()
        }
        payloads["fresh/output/runtime_lock.json"] = (
            json.dumps(
                {
                    "schema_version": 1,
                    "contract": "ams.m0.runtime-lock-verification/v1",
                    "passed": False,
                    "observed_image_digest": self.fixture.image_digest,
                    "lock_sha256": self.fixture.execution_contract[
                        "dependency_lock_sha256"
                    ],
                    "checks": {},
                    "failures": ["explicit forged failure"],
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        details = self.fixture.receipt["gates"]["host_final"]["details"]
        failures = _validate_fresh_raw(
            self.fixture.root,
            payloads,
            details["fresh_exact_image_reexecution"],
            run_id=self.fixture.run_id,
            image_digest=self.fixture.image_digest,
            source_commit=self.fixture.base,
            execution_contract=self.fixture.execution_contract,
            expected_vector=self.fixture.vector,
            plan_sha256=self.fixture.plan_sha256,
        )
        self.assertTrue(
            any("runtime-lock raw report did not pass exactly" in item for item in failures),
            failures,
        )

    def test_source_binding_is_rederived_from_execution_git_objects(self) -> None:
        def mutate(receipt: dict[str, Any]) -> None:
            details = receipt["gates"]["host_final"]["details"]
            details["source_before"]["source_binding_sha256"] = "e" * 64
            details["source_after"]["source_binding_sha256"] = "e" * 64
            self.fixture.refresh_qualification_contract_hash()

        self.fixture.rewrite_receipt(mutate)
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("source snapshot identities" in item for item in result["failures"]), result)

    def test_privileged_raw_container_cannot_hide_behind_receipt_hashes(self) -> None:
        details = self.fixture.receipt["gates"]["host_final"]["details"]
        parsed: dict[str, tuple[bytes, dict[str, Any]]] = {}
        for name in (
            "retained/initial_container_inspect.json",
            "retained/final_container_inspect.json",
        ):
            path = self.fixture.root / f"runs/{self.fixture.run_id}/host_validation/{name}"
            path.chmod(0o600)
            document = json.loads(path.read_text(encoding="utf-8"))
            document[0]["HostConfig"]["Privileged"] = True
            raw = json.dumps(document, sort_keys=True).encode("utf-8")
            path.write_bytes(raw)
            path.chmod(0o444)
            parsed[name] = (raw, document[0])
            self.fixture.receipt["host_validation_raw"]["files"][name] = {
                "bytes": len(raw),
                "sha256": sha256(raw),
                "published_mode": 0o400,
            }
        fingerprint = sha256(
            canonical(
                _container_immutable_fingerprint(
                    parsed["retained/final_container_inspect.json"][1]
                )
            )
        )
        for retained_name in (
            "retained_container_initial_final",
            "retained_container_reinspection",
        ):
            retained = details[retained_name]
            retained["initial_container_inspect_sha256"] = sha256(
                parsed["retained/initial_container_inspect.json"][0]
            )
            retained["final_container_inspect_sha256"] = sha256(
                parsed["retained/final_container_inspect.json"][0]
            )
            retained["immutable_fingerprint_sha256"] = fingerprint
            retained["raw_sha256"]["retained/initial_container_inspect.json"] = sha256(
                parsed["retained/initial_container_inspect.json"][0]
            )
            retained["raw_sha256"]["retained/final_container_inspect.json"] = sha256(
                parsed["retained/final_container_inspect.json"][0]
            )
        self.fixture.rewrite_receipt(lambda _receipt: None)
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("HostConfig isolation" in item for item in result["failures"]), result)

    def test_unmanifested_host_control_file_is_rejected(self) -> None:
        control = self.fixture.root / f"runs/{self.fixture.run_id}/host_validation"
        control.chmod(0o700)
        extra = control / "unmanifested.bin"
        extra.write_bytes(b"forged")
        extra.chmod(0o444)
        control.chmod(0o500)
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("extra/missing" in item for item in result["failures"]), result)

    def test_missing_published_artifact_is_rejected(self) -> None:
        logs = self.fixture.root / f"runs/{self.fixture.run_id}/logs"
        logs.chmod(0o700)
        (logs / "captured.log").unlink()
        logs.chmod(0o500)
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("portable artifact" in item for item in result["failures"]), result)

    def test_nonfinite_receipt_json_fails_as_machine_readable_result(self) -> None:
        self.fixture.rewrite_receipt(
            lambda receipt: receipt["gates"]["runtime_lock"]["details"].update(
                {"forged": float("nan")}
            )
        )
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("non-finite" in item for item in result["failures"]), result)

    def test_malformed_host_details_fail_closed_without_exception(self) -> None:
        self.fixture.rewrite_receipt(
            lambda receipt: receipt["gates"]["host_final"].update({"details": []})
        )
        result = validate_live_status(self.fixture.root)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["passed"], result)
        self.assertTrue(result["failures"], result)

    def test_fresh_reexecution_container_must_be_distinct(self) -> None:
        self.fixture.rewrite_receipt(
            lambda receipt: receipt["gates"]["host_final"]["details"][
                "fresh_exact_image_reexecution"
            ].update({"container_id": self.fixture.container_id})
        )
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("fresh exact-image" in item for item in result["failures"]), result)

    def test_m0_receipt_cannot_self_certify_m1_through_m8(self) -> None:
        self.fixture.metadata["state"] = {
            "active_milestone": None,
            "customer_ready": True,
            "fully_closed_sequential_milestones": 9,
        }
        self.fixture.metadata["next_command"] = {
            "milestone": None,
            "argv": [],
            "argv_sha256": sha256(canonical([])),
            "tracked_inputs": [],
        }
        rows = "".join(f"| M{index} | `passed` | forged |\n" for index in range(9))
        block = canonical_status_metadata_block(self.fixture.metadata)
        self.fixture._write(
            "network/PROGRESS.md",
            rows
            + "Fully closed sequential milestones: **9**\nCustomer-ready: **true**\n\n"
            + block
            + "\n",
        )
        self.fixture._write(
            "network/VALIDATION_REPORT.md",
            rows
            + "Fully closed milestones: **9**\nCustomer-ready: **true**\n\n"
            + block
            + "\n",
        )
        self.fixture._write(
            "network/NEXT_TASK.md",
            "Fully closed milestones: **9**\nCustomer-ready: **true**\n\n"
            + block
            + "\n",
        )
        self.fixture._git("add", *STATUS_PATHS)
        self.fixture._git("commit", "-qm", "attempt unsupported downstream closure")
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("authorizes only" in item for item in result["failures"]), result)

    def test_nonexecutable_document_cannot_be_the_next_command(self) -> None:
        argv = ["doc/network_radio_integration_plan_v3.md"]
        self.fixture.metadata["next_command"] = {
            "milestone": "M1",
            "argv": argv,
            "argv_sha256": sha256(canonical(argv)),
            "tracked_inputs": [
                _git_blob_record(
                    self.fixture.root,
                    self.fixture.base,
                    "doc/network_radio_integration_plan_v3.md",
                )
            ],
        }
        self.fixture.rewrite_metadata()
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("canonical formal command" in item for item in result["failures"]), result)
        self.assertTrue(any("must be executable" in item for item in result["failures"]), result)

    def test_receipt_symlink_and_hardlink_are_rejected(self) -> None:
        with self.subTest("symlink"):
            original = self.fixture.receipt_path.with_name("receipt.real")
            self.fixture.receipt_path.parent.chmod(0o700)
            self.fixture.receipt_path.chmod(0o600)
            self.fixture.receipt_path.rename(original)
            self.fixture.receipt_path.symlink_to(original.name)
            result = validate_live_status(self.fixture.root)
            self.assertFalse(result["passed"], result)
            self.assertTrue(any("secure read" in item for item in result["failures"]), result)
        self.tearDown()
        self.setUp()
        with self.subTest("hardlink"):
            alias = self.fixture.receipt_path.with_name("receipt.alias")
            self.fixture.receipt_path.parent.chmod(0o700)
            os.link(self.fixture.receipt_path, alias)
            result = validate_live_status(self.fixture.root)
            self.assertFalse(result["passed"], result)
            self.assertTrue(any("single-link" in item for item in result["failures"]), result)

    def test_status_symlink_is_rejected_even_at_clean_head(self) -> None:
        progress = self.fixture.root / STATUS_PATHS[0]
        target = self.fixture.root / "network/progress-target.md"
        target.write_bytes(progress.read_bytes())
        progress.unlink()
        progress.symlink_to("progress-target.md")
        self.fixture._git("add", STATUS_PATHS[0], "network/progress-target.md")
        self.fixture._git("commit", "-qm", "attempt status symlink")
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("secure read" in item for item in result["failures"]), result)

    def test_dirty_checkout_is_rejected_before_status_authority(self) -> None:
        with (self.fixture.root / STATUS_PATHS[2]).open("a", encoding="utf-8") as output:
            output.write("dirty\n")
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertIn("live status lint requires a clean HEAD", result["failures"])

    def test_conflicting_second_active_milestone_is_rejected(self) -> None:
        for relative in (STATUS_PATHS[0], STATUS_PATHS[2]):
            path = self.fixture.root / relative
            with path.open("a", encoding="utf-8") as output:
                output.write("Active milestone: **M8**\n")
        self.fixture._git("add", STATUS_PATHS[0], STATUS_PATHS[2])
        self.fixture._git("commit", "-qm", "conflicting active milestone")
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("active milestone disagrees" in item for item in result["failures"]), result)

    def test_nonancestor_technical_base_is_rejected(self) -> None:
        report_head = self.fixture._git("rev-parse", "HEAD").stdout.strip()
        self.fixture._git("switch", "-q", "-c", "unrelated", self.fixture.base)
        self.fixture._write("unrelated.txt", "sibling\n")
        self.fixture._git("add", "unrelated.txt")
        self.fixture._git("commit", "-qm", "unrelated sibling")
        unrelated = self.fixture._git("rev-parse", "HEAD").stdout.strip()
        self.fixture._git("switch", "-q", "master")
        self.assertEqual(self.fixture._git("rev-parse", "HEAD").stdout.strip(), report_head)
        self.fixture.metadata["technical_base_commit"] = unrelated
        self.fixture.rewrite_metadata()
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("not an ancestor" in item for item in result["failures"]), result)

    def test_fourth_changed_path_removes_status_only_exemption(self) -> None:
        self.fixture._write("fourth-path.txt", "not report-only\n")
        self.fixture._git("add", "fourth-path.txt")
        self.fixture._git("commit", "-qm", "mix technical path")
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("exactly three" in item for item in result["failures"]), result)

    def test_mismatched_document_citation_is_rejected(self) -> None:
        path = self.fixture.root / STATUS_PATHS[0]
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(self.fixture.run_id, "m0_different_run", 1), encoding="utf-8")
        self.fixture._git("add", STATUS_PATHS[0])
        self.fixture._git("commit", "-qm", "mismatched citation")
        result = validate_live_status(self.fixture.root)
        self.assertFalse(result["passed"], result)
        self.assertTrue(any("do not share exact metadata" in item for item in result["failures"]), result)

    def test_cli_has_no_arbitrary_path_override(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--progress", "/tmp/forged.md"])


if __name__ == "__main__":
    unittest.main()
