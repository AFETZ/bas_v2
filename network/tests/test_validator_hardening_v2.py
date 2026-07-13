#!/usr/bin/env python3
"""Adversarial tests for M0 fail-closed and immutable-evidence rules."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import (  # noqa: E402
    _raw_experiment,
    artifact_status,
    delivery_status,
    five_uav_health_status,
    joint_runtime_status,
    no_bypass_status,
    ns3_build_receipt_evidence_status,
    packet_provenance_status,
    pcap_stats,
)


def write_pcap(path: Path, frame: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        handle.write(struct.pack("<IIII", 1, 0, len(frame), len(frame)))
        handle.write(frame)


def prepare_complete_raw_fixture(run_dir: Path) -> tuple[Path, tuple[str, ...]]:
    matrix_path = ROOT_DIR / "network/config/validation_matrix.yaml"
    matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    required = tuple(matrix["run_outputs"]["raw_runtime_required"])
    for relative in required:
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{relative}\n", encoding="utf-8")
    (run_dir / "metrics/provenance.json").write_text(
        json.dumps({"source_hash": "a" * 64}), encoding="utf-8"
    )
    (run_dir / "metrics/joint_runtime.json").write_text(
        json.dumps({"runtime_id": "runtime-fixture"}), encoding="utf-8"
    )
    return matrix_path, required


def run_sealer(run_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "network/scripts/seal_run_evidence.py"),
            "--run-dir",
            str(run_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


JOINT_COMPONENTS = (
    "gazebo",
    "ardupilot",
    "position_tracker",
    "bridge",
    "ns3",
    "sionna",
    "traffic_endpoints",
)


def write_joint_runtime_fixture(
    run_dir: Path,
    *,
    raw_relative: str = "logs/joint_runtime_events.jsonl",
    complete_envelope: bool = True,
    append_unexpected_after_completion: bool = False,
) -> None:
    source_hash = "a" * 64
    runtime_id = "runtime-joint-fixture"
    (run_dir / "metrics").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics/provenance.json").write_text(
        json.dumps({"source_hash": source_hash}), encoding="utf-8"
    )
    events: list[dict[str, object]] = [
        {"event": "joint_runtime_start", "_clock": 0}
    ]
    for tick in range(62):
        for component_index, component in enumerate(JOINT_COMPONENTS):
            events.append(
                {
                    "event": "component_sample",
                    "component": component,
                    "ready": True,
                    "healthy": True,
                    "pid": 100 + component_index,
                    "_clock": 1_000_000 + tick * 5_000_000_000 + component_index,
                }
            )
    completion_clock = 305_002_000_000
    events.append(
        {
            "event": "joint_runtime_complete",
            "errors": [],
            "_clock": completion_clock,
        }
    )
    if append_unexpected_after_completion:
        events.append({"event": "foreign_event", "_clock": completion_clock + 1_000_000})
    for event_seq, event in enumerate(events, start=1):
        clock = event.pop("_clock", event_seq * 1_000_000)
        if complete_envelope or event.get("event") in {
            "joint_runtime_start",
            "joint_runtime_complete",
        }:
            event.update(
                {
                    "schema_version": 2,
                    "run_id": run_dir.name,
                    "runtime_id": runtime_id,
                    "source_hash": source_hash,
                    "event_seq": event_seq,
                    "monotonic_ns": clock,
                    "wall_utc": "2026-07-12T12:00:00Z",
                }
            )
    raw = "".join(json.dumps(event) + "\n" for event in events)
    raw_path = run_dir / raw_relative
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw, encoding="utf-8")
    summary = {
        "schema_version": 2,
        "run_id": run_dir.name,
        "runtime_id": runtime_id,
        "source_hash": source_hash,
        "required_overlap_s": 305.0,
        "components": [
            {"name": component, "ready": True, "healthy": True, "exit_code": None}
            for component in JOINT_COMPONENTS
        ],
        "errors": [],
        "raw_event_log": raw_relative,
        "raw_event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }
    (run_dir / "metrics/joint_runtime.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )


class ValidatorHardeningV2Tests(unittest.TestCase):
    def test_integrated_ns3_receipt_is_content_checked(self) -> None:
        from network.ns3.ns3_build_receipt import subject_digest

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "receipt"
            receipt_path = run_dir / "metrics/ns3_tap_build_receipt.json"
            receipt_path.parent.mkdir(parents=True)
            source_hash = hashlib.sha256(
                (ROOT_DIR / "network/ns3/scratch/ams-tap-vertical-slice.cc").read_bytes()
            ).hexdigest()
            modules = [
                "applications",
                "bridge",
                "core",
                "csma",
                "flow-monitor",
                "internet",
                "mobility",
                "network",
                "stats",
                "tap-bridge",
                "traffic-control",
            ]
            subject = {
                "program": "ams-tap-vertical-slice",
                "official_source": {
                    "root": "/workspace/multiagent_simulation/.external/ns-3",
                    "version": "3.40",
                    "core_tree_files": 3764,
                    "core_tree_sha256": "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8",
                },
                "scratch_source": {
                    "project": {
                        "path": "/workspace/multiagent_simulation/network/ns3/scratch/ams-tap-vertical-slice.cc",
                        "sha256": source_hash,
                    },
                    "copied": {
                        "path": "/workspace/multiagent_simulation/.external/ns-3/scratch/ams-tap-vertical-slice.cc",
                        "sha256": source_hash,
                    },
                    "byte_identical": True,
                },
                "build": {"enabled_modules": modules, "required_modules": modules},
                "executable": {
                    "path": "/workspace/multiagent_simulation/.external/ns-3/build/scratch/ns3.40-ams-tap-vertical-slice-default",
                    "sha256": "e" * 64,
                    "size_bytes": 1024,
                    "mode": 0o755,
                },
            }
            document = {
                "schema_version": 1,
                "contract": "ams.ns3.build-receipt/v1",
                "created_utc": "2026-07-12T12:00:00Z",
                "subject_sha256": subject_digest(subject),
                "subject": subject,
            }
            receipt_path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(
                ns3_build_receipt_evidence_status(run_dir)["status"], "passed"
            )
            document["subject"]["executable"]["sha256"] = "f" * 64
            receipt_path.write_text(json.dumps(document), encoding="utf-8")
            result = ns3_build_receipt_evidence_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("subject digest", "\n".join(result["details"]["failures"]))

    def test_impossible_delivery_counters_and_latencies_fail(self) -> None:
        summary = {
            "packets": {
                "control_tx": 1,
                "control_rx": 2,
                "payload_tx": 1,
                "payload_rx": 2,
                "additional_tx": 1,
                "additional_rx": 2,
            },
            "loss_rate": {"control": -1.0, "payload": -1.0, "additional_data": -1.0},
            "latency_ms": {
                "control_p50": -1.0,
                "control_p95": -2.0,
                "payload_p50": 20.0,
                "payload_p95": 10.0,
            },
        }
        result = delivery_status(summary)
        self.assertFalse(result["passed"])
        failures = "\n".join(result["failures"])
        self.assertIn("exceeds transmitted", failures)
        self.assertIn("outside [0, 1]", failures)
        self.assertIn("negative", failures)
        self.assertIn("p95 latency is below p50", failures)

    def test_nan_and_bare_booleans_cannot_pass_causal_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "forged"
            (run_dir / "metrics").mkdir(parents=True)
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": "a" * 64}), encoding="utf-8"
            )
            (run_dir / "metrics/priority_experiment.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": "forged-runtime",
                        "source_hash": "a" * 64,
                        "offered_load_at_least_2x_capacity": True,
                        "payload_degraded_before_control": True,
                        "ns3_owned_priority": True,
                        "control_p95_ms": float("nan"),
                        "control_loss_rate": -1.0,
                    }
                ),
                encoding="utf-8",
            )
            result = _raw_experiment(
                run_dir,
                "metrics/priority_experiment.json",
                "priority",
                (
                    "offered_load_at_least_2x_capacity",
                    "payload_degraded_before_control",
                    "ns3_owned_priority",
                ),
                ("overload_offer", "control_delivery"),
                numeric_maximums={"control_p95_ms": 250.0, "control_loss_rate": 0.05},
            )
            self.assertEqual(result["status"], "failed")
            self.assertIn("raw evidence", "\n".join(result["details"]["failures"]))

    def test_nonce_in_ethernet_padding_is_not_counted(self) -> None:
        ethernet = b"\x10" * 12 + struct.pack("!H", 0x0800)
        payload = b"x"
        total_length = 20 + 8 + len(payload)
        ipv4 = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            total_length,
            1,
            0,
            64,
            17,
            0,
            b"\x0a\x00\x00\x01",
            b"\x0a\x00\x00\x02",
        )
        udp = struct.pack("!HHHH", 1000, 1001, 9, 0)
        frame = ethernet + ipv4 + udp + payload + b"nonce-in-padding"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "padding.pcap"
            write_pcap(path, frame)
            self.assertEqual(pcap_stats(path, nonce="nonce-in-padding")["nonce_hits"], 0)

    def test_joint_runtime_requires_fixed_sealed_path_and_full_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            positive = Path(temp) / "positive"
            write_joint_runtime_fixture(positive)
            self.assertEqual(joint_runtime_status(positive)["status"], "passed")

        cases = (
            ("alternate_path", {"raw_relative": "logs/alternate-unsealed.jsonl"}),
            ("missing_envelope", {"complete_envelope": False}),
            ("bad_boundary_and_event", {"append_unexpected_after_completion": True}),
        )
        for name, options in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                run_dir = Path(temp) / name
                write_joint_runtime_fixture(run_dir, **options)

                result = joint_runtime_status(run_dir)

                self.assertEqual(result["status"], "failed")
                failures = "\n".join(result["details"]["failures"])
                if name == "alternate_path":
                    self.assertIn("raw_event_log must be logs/joint_runtime_events.jsonl", failures)
                elif name == "missing_envelope":
                    self.assertIn("raw-event envelope", failures)
                else:
                    self.assertIn("unexpected event types", failures)
                    self.assertIn("do not bound", failures)

    def test_summary_controlled_raw_paths_cannot_escape_sealed_matrix_paths(self) -> None:
        source_hash = "d" * 64
        runtime_id = "runtime-path-fixture"
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "paths"
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "metrics").mkdir(parents=True)
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": source_hash}), encoding="utf-8"
            )
            (run_dir / "metrics/joint_runtime.json").write_text(
                json.dumps({"runtime_id": runtime_id}), encoding="utf-8"
            )
            alternate = run_dir / "logs/alternate.jsonl"
            alternate.write_text("{}\n", encoding="utf-8")
            alternate_hash = hashlib.sha256(alternate.read_bytes()).hexdigest()

            (run_dir / "metrics/five_uav_health.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": runtime_id,
                        "source_hash": source_hash,
                        "raw_event_log": "logs/alternate.jsonl",
                        "raw_event_sha256": alternate_hash,
                    }
                ),
                encoding="utf-8",
            )
            five = five_uav_health_status(run_dir)
            self.assertIn(
                "five-UAV raw_event_log must be logs/five_uav_health_events.jsonl",
                "\n".join(five["details"]["failures"]),
            )

            (run_dir / "logs/no_bypass_active.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": runtime_id,
                        "source_hash": source_hash,
                        "run_nonce": "path-fixture",
                        "raw_event_log": "logs/alternate.jsonl",
                        "raw_event_sha256": alternate_hash,
                    }
                ),
                encoding="utf-8",
            )
            no_bypass = no_bypass_status(run_dir)
            self.assertIn(
                "no-bypass raw_event_log must be logs/no_bypass_events.jsonl",
                "\n".join(no_bypass["details"]["failures"]),
            )

            (run_dir / "metrics/packet_provenance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": runtime_id,
                        "source_hash": source_hash,
                        "run_nonce": "path-fixture",
                        "raw_mavlink_log": "logs/alternate.jsonl",
                        "raw_mavlink_sha256": alternate_hash,
                    }
                ),
                encoding="utf-8",
            )
            packet = packet_provenance_status(
                run_dir, {"failures": []}, {"failures": []}
            )
            self.assertIn(
                "packet provenance raw_mavlink_log must be logs/mavlink_transactions.jsonl",
                "\n".join(packet["details"]["failures"]),
            )

    def test_artifact_seal_detects_raw_mutation_and_excludes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "sealed"
            matrix_path = ROOT_DIR / "network/config/validation_matrix.yaml"
            matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
            required = tuple(matrix["run_outputs"]["raw_runtime_required"])
            for relative in required:
                raw_path = run_dir / relative
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(f"fixture:{relative}\n", encoding="utf-8")
            source_hash = "b" * 64
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": source_hash}), encoding="utf-8"
            )
            (run_dir / "metrics/joint_runtime.json").write_text(
                json.dumps({"runtime_id": "runtime-sealed"}), encoding="utf-8"
            )
            manifest = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": "runtime-sealed",
                "source_hash": source_hash,
                "sealed_utc": "2026-07-12T12:00:00Z",
                "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
                "files": {
                    relative: {
                        "sha256": hashlib.sha256((run_dir / relative).read_bytes()).hexdigest(),
                        "size_bytes": (run_dir / relative).stat().st_size,
                    }
                    for relative in required
                },
            }
            (run_dir / "metrics/evidence_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            for relative in required:
                (run_dir / relative).chmod(0o444)
            (run_dir / "metrics/evidence_manifest.json").chmod(0o444)
            attested = {
                "status": "passed",
                "proof": "fixture external attestation",
                "details": {},
            }
            receipt = {
                "status": "passed",
                "proof": "fixture ns-3 receipt",
                "details": {},
            }
            with mock.patch(
                "network.validation.evidence.evidence_attestation_status",
                return_value=attested,
            ), mock.patch(
                "network.validation.evidence.ns3_build_receipt_evidence_status",
                return_value=receipt,
            ):
                self.assertEqual(artifact_status(run_dir, matrix_path)["status"], "passed")
                (run_dir / "metrics/summary.json").write_text("{}\n", encoding="utf-8")
                self.assertEqual(artifact_status(run_dir, matrix_path)["status"], "passed")
                raw_path = run_dir / "command.txt"
                raw_path.chmod(0o644)
                raw_path.write_text("changed\n", encoding="utf-8")
                self.assertEqual(artifact_status(run_dir, matrix_path)["status"], "failed")

    def test_manifest_missing_timestamp_and_writable_reseal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "schema_fail"
            matrix_path = ROOT_DIR / "network/config/validation_matrix.yaml"
            matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
            required = tuple(matrix["run_outputs"]["raw_runtime_required"])
            for relative in required:
                path = run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("raw\n", encoding="utf-8")
                path.chmod(0o444)
            (run_dir / "metrics/provenance.json").chmod(0o644)
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": "c" * 64}), encoding="utf-8"
            )
            (run_dir / "metrics/provenance.json").chmod(0o444)
            (run_dir / "metrics/joint_runtime.json").chmod(0o644)
            (run_dir / "metrics/joint_runtime.json").write_text(
                json.dumps({"runtime_id": "runtime-schema"}), encoding="utf-8"
            )
            (run_dir / "metrics/joint_runtime.json").chmod(0o444)
            manifest = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": "runtime-schema",
                "source_hash": "c" * 64,
                "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
                "files": {
                    relative: {
                        "sha256": hashlib.sha256((run_dir / relative).read_bytes()).hexdigest(),
                        "size_bytes": (run_dir / relative).stat().st_size,
                    }
                    for relative in required
                },
            }
            manifest_path = run_dir / "metrics/evidence_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = artifact_status(run_dir, matrix_path)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("fields differ", failures)
            self.assertIn("sealed_utc", failures)
            self.assertIn("remains writable", failures)

    def test_sealer_rejects_hardlinks_and_symlinked_parent_directories(self) -> None:
        for alias_kind in ("hardlink", "symlink_parent"):
            with self.subTest(alias_kind=alias_kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run_dir = root / "aliased"
                prepare_complete_raw_fixture(run_dir)
                if alias_kind == "hardlink":
                    command = run_dir / "command.txt"
                    content = command.read_bytes()
                    command.unlink()
                    foreign = root / "foreign-command.txt"
                    foreign.write_bytes(content)
                    os.link(foreign, command)
                else:
                    logs = run_dir / "logs"
                    real_logs = run_dir / "real_logs"
                    logs.rename(real_logs)
                    logs.symlink_to(real_logs.name, target_is_directory=True)

                result = run_sealer(run_dir)

                self.assertEqual(result.returncode, 2)
                self.assertFalse((run_dir / "metrics/evidence_manifest.json").exists())
                expected = "hard links" if alias_kind == "hardlink" else "symbolic-link component"
                self.assertIn(expected, result.stderr)

    def test_artifact_gate_rejects_post_seal_hardlink_and_symlink_parent(self) -> None:
        for alias_kind in ("hardlink", "symlink_parent"):
            with self.subTest(alias_kind=alias_kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run_dir = root / "sealed"
                prepare_complete_raw_fixture(run_dir)
                sealed = run_sealer(run_dir)
                self.assertEqual(sealed.returncode, 0, sealed.stderr)
                if alias_kind == "hardlink":
                    command = run_dir / "command.txt"
                    content = command.read_bytes()
                    command.unlink()
                    foreign = root / "foreign-command.txt"
                    foreign.write_bytes(content)
                    foreign.chmod(0o444)
                    os.link(foreign, command)
                else:
                    logs = run_dir / "logs"
                    real_logs = run_dir / "real_logs"
                    logs.rename(real_logs)
                    logs.symlink_to(real_logs.name, target_is_directory=True)

                result = artifact_status(run_dir)

                self.assertEqual(result["status"], "failed")
                failures = "\n".join(result["details"]["failures"])
                expected = "hard links" if alias_kind == "hardlink" else "symbolic-link component"
                self.assertIn(expected, failures)

    def test_empty_custom_matrix_cannot_seal_or_validate_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "empty_matrix"
            (run_dir / "metrics").mkdir(parents=True)
            custom_matrix = root / "matrix.yaml"
            custom_matrix.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 2,
                        "plan": "doc/network_radio_integration_plan_v2.md",
                        "run_outputs": {
                            "raw_runtime_required": [],
                            "validator_outputs": [],
                            "raw_seal": "metrics/evidence_manifest.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "network/scripts/seal_run_evidence.py"),
                    "--run-dir",
                    str(run_dir),
                    "--matrix",
                    str(custom_matrix),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("not byte-identical", result.stderr)
            self.assertFalse((run_dir / "metrics/evidence_manifest.json").exists())
            self.assertEqual(artifact_status(run_dir, custom_matrix)["status"], "failed")

            validation = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "network/validation/validate_run.py"),
                    "--run-dir",
                    str(run_dir),
                    "--matrix",
                    str(custom_matrix),
                    "--no-write",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(validation.returncode, 2)
            self.assertIn("not byte-identical", validation.stderr)

    def test_validator_refuses_json_output_outside_run_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            outside = Path(temp) / "raw.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "network/validation/validate_run.py"),
                    "--run-dir",
                    str(run_dir),
                    "--json-output",
                    str(outside),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(outside.exists())
            self.assertIn("must stay under", result.stderr)


if __name__ == "__main__":
    unittest.main()
