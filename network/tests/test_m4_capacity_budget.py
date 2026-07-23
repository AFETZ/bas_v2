#!/usr/bin/env python3
"""Adversarial tests for the independent M4 capacity execution budget."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import Any

from network.ns3.tap_packet_engine_config import CONTRACT as ENGINE_CONTRACT
from network.ns3.tap_packet_engine_config import from_repository
from network.validation.m4_capacity_budget import (
    AIRBORNE_STATE_WAIT_NS,
    BOUNDED_PREFLIGHT_NS,
    CONTRACT_TO_CLEAN_SHUTDOWN_NS,
    ENDPOINTS_PATH,
    EXPECTED_EXECUTION_BUDGET,
    EXPECTED_STAGE_TIMING_BUDGET,
    NS3_ENGINE_DURATION_NS,
    PROFILE_PATH,
    RADIO_PATH,
    READINESS_RESERVE_NS,
    READINESS_RUNWAY_NS,
    RUNNER_PATH,
    WRAPPER_TIMEOUT_NS,
    execution_budget_derivation,
    validate_capacity_execution_budget,
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class CapacityExecutionBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = (Path(self.temporary.name) / "run").resolve()
        (self.run_dir / "logs").mkdir(parents=True)
        (self.run_dir / "raw/state").mkdir(parents=True)
        self.runtime_id = "a" * 32
        self.binary_path = "/qualified/ns3.40-ams-tap-packet-engine-default"
        self.binary_sha256 = "b" * 64
        self.created_ns = 1_000_000_000
        warmup_start = self.created_ns + READINESS_RUNWAY_NS
        measurement_start = warmup_start + 30_000_000_000
        measurement_end = measurement_start + 600_000_000_000
        self.run: dict[str, Any] = {
            "runtime_id": self.runtime_id,
            "created_monotonic_ns": self.created_ns,
            "execution_budget": copy.deepcopy(EXPECTED_EXECUTION_BUDGET),
            "schedule": {
                "readiness_deadline_monotonic_ns": warmup_start,
                "warmup_start_monotonic_ns": warmup_start,
                "measurement_start_monotonic_ns": measurement_start,
                "measurement_end_monotonic_ns": measurement_end,
                "readiness_stability_ns": 10_000_000_000,
                "warmup_ns": 30_000_000_000,
                "measurement_ns": 600_000_000_000,
            },
            "airborne_gate": {
                "warmup_start_monotonic_ns": warmup_start,
                "measurement_start_monotonic_ns": measurement_start,
                "measurement_end_monotonic_ns": measurement_end,
                "airborne_ready_deadline_monotonic_ns": warmup_start,
                "stage_timing_budget": copy.deepcopy(
                    EXPECTED_STAGE_TIMING_BUDGET
                ),
            },
            "identity": {
                "executable_manifest": {
                    "ns3_packet_engine": {
                        "path": self.binary_path,
                        "sha256": self.binary_sha256,
                        "size_bytes": 123,
                    }
                }
            },
        }
        self.engine_argv = self._write_engine_evidence()
        self._write_runtime_sample(self._expected_cmdline_sha256())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _engine_config(self):
        return from_repository(
            uav_count=5,
            duration_ms=NS3_ENGINE_DURATION_NS // 1_000_000,
            seed=42,
            run=1,
            event_epoch=1,
            self_test=False,
            self_test_burst=1,
            self_test_unknown_tos=False,
            tap_gcs="tap-gcs",
            tap_uavs=tuple(f"tap-uav{index}" for index in range(1, 6)),
            sionna_ipc_enabled=True,
            sionna_state_file=str(
                self.run_dir / "logs/sionna_applied_states.jsonl"
            ),
            sionna_poll_interval_ms=1,
            sionna_max_updates_per_poll=64,
            sionna_max_state_ttl_ms=2000,
            sionna_intervention="natural",
            clock_datagram_socket=f"/tmp/ams-m4-clock-{self.runtime_id}.sock",
            endpoints_path=ENDPOINTS_PATH,
            radio_path=RADIO_PATH,
        )

    def _write_engine_evidence(self) -> list[str]:
        config = self._engine_config()
        engine_argv = config.engine_argv(
            events_file=str(self.run_dir / "logs/ns3_packet_events.jsonl"),
            pcap_prefix=str(self.run_dir / "pcap/ns3-packet-engine"),
        )
        report = {
            "contract": ENGINE_CONTRACT,
            "config_sha256": config.sha256(),
            "canonical_config": config.canonical_text(),
            "resolved": {**asdict(config), "tap_uavs": list(config.tap_uavs)},
            "engine_argv": engine_argv,
            "source_sha256": {
                str(ENDPOINTS_PATH): hashlib.sha256(
                    ENDPOINTS_PATH.read_bytes()
                ).hexdigest(),
                str(RADIO_PATH): hashlib.sha256(RADIO_PATH.read_bytes()).hexdigest(),
            },
        }
        write_json(self.run_dir / "logs/ns3_packet_engine_config.json", report)
        (self.run_dir / "logs/ns3_packet_engine.argv").write_text(
            "\n".join(engine_argv) + "\n", encoding="utf-8"
        )
        write_json(
            self.run_dir / "raw/state/ns3-engine.ready.json",
            {
                "status": "ready",
                "contract": ENGINE_CONTRACT,
                "config_sha256": config.sha256(),
                "event_epoch": 1,
                "uav_count": 5,
            },
        )
        return engine_argv

    def _expected_cmdline_sha256(self) -> str:
        full_argv = [
            self.binary_path,
            *self.engine_argv,
            f"--readyFile={self.run_dir / 'raw/state/ns3-engine.ready.json'}",
            f"--stopFile={self.run_dir / 'raw/control/ns3-engine.stop'}",
        ]
        raw = b"\0".join(value.encode("utf-8") for value in full_argv) + b"\0"
        return hashlib.sha256(raw).hexdigest()

    def _write_runtime_sample(self, cmdline_sha256: str) -> None:
        process = {
            "pid": 101,
            "start_ticks": 202,
            "pgid": 101,
            "role": "ns3_packet_engine",
            "executable_path": self.binary_path,
            "executable_sha256": self.binary_sha256,
            "cmdline_sha256": cmdline_sha256,
        }
        path = self.run_dir / "logs/m4_runtime_events.jsonl"
        path.write_text(
            json.dumps(
                {
                    "event": "measurement_resource_sample",
                    "processes": {"processes": [process]},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _validate(self, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
        return validate_capacity_execution_budget(
            self.run_dir, self.run, **kwargs
        )

    def test_exact_budget_binds_schedule_engine_and_live_process(self) -> None:
        details, failures = self._validate()
        self.assertEqual(failures, [])
        self.assertEqual(BOUNDED_PREFLIGHT_NS, 279_000_000_000)
        self.assertEqual(READINESS_RESERVE_NS, 25_500_000_000)
        self.assertEqual(CONTRACT_TO_CLEAN_SHUTDOWN_NS, 1_540_000_000_000)
        self.assertEqual(NS3_ENGINE_DURATION_NS, 1_600_000_000_000)
        self.assertEqual(WRAPPER_TIMEOUT_NS, 1_800_000_000_000)
        self.assertEqual(AIRBORNE_STATE_WAIT_NS, 60_000_000_000)
        self.assertEqual(details["engine"]["duration_ms"], 1_600_000)
        self.assertTrue(details["engine"]["ready_config_bound"])
        self.assertEqual(details["sampled_engine_process"]["sample_count"], 1)
        derivation = execution_budget_derivation()
        self.assertEqual(derivation["pre_measurement_stage_count"], 8)
        self.assertEqual(derivation["extended_sys_state_maximum_attempts"], 6)
        self.assertEqual(derivation["extended_sys_state_execution_ns"], 33_000_000_000)
        self.assertEqual(derivation["reused_command_guard_count"], 3)
        self.assertEqual(derivation["reused_command_guard_total_ns"], 9_000_000_000)

    def test_every_declared_budget_value_is_exact(self) -> None:
        for key, value in EXPECTED_EXECUTION_BUDGET.items():
            if key == "contract":
                replacement: Any = str(value) + "-forged"
            else:
                replacement = int(value) + 1
            with self.subTest(key=key):
                run = copy.deepcopy(self.run)
                run["execution_budget"][key] = replacement
                _details, failures = validate_capacity_execution_budget(
                    self.run_dir, run
                )
                self.assertTrue(
                    any("execution budget differs" in item for item in failures),
                    failures,
                )

    def test_extra_budget_key_and_shifted_schedule_fail(self) -> None:
        self.run["execution_budget"]["forged_reserve_ns"] = 1
        self.run["schedule"]["measurement_start_monotonic_ns"] += 1
        _details, failures = self._validate()
        self.assertTrue(any("keys differ" in item for item in failures), failures)
        self.assertTrue(any("720+30+600" in item for item in failures), failures)

    def test_component_profile_timeout_is_independently_bound(self) -> None:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        profile["profiles"]["m4_capacity_prerequisite"]["timeout_s"] = 1799
        path = self.run_dir / "forged_profiles.json"
        write_json(path, profile)
        _details, failures = self._validate(profiles_path=path)
        self.assertTrue(any("component profile budget" in item for item in failures))

    def test_runner_duration_assignment_and_injection_are_exact(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8").replace(
            "CAPACITY_NS3_DURATION_MS=1600000",
            "CAPACITY_NS3_DURATION_MS=1599999",
        )
        path = self.run_dir / "run_m4_capacity.sh"
        path.write_text(source, encoding="utf-8")
        _details, failures = self._validate(runner_path=path)
        self.assertTrue(any("runner duration source" in item for item in failures))

    def test_rebuilt_engine_config_rejects_duration_or_ttl_mutation(self) -> None:
        path = self.run_dir / "logs/ns3_packet_engine_config.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report["resolved"]["duration_ms"] -= 1
        report["resolved"]["sionna_max_state_ttl_ms"] += 1
        write_json(path, report)
        _details, failures = self._validate()
        self.assertTrue(any("rebuilt EngineConfig" in item for item in failures))

    def test_argv_log_and_ready_hash_are_not_replaceable(self) -> None:
        argv_path = self.run_dir / "logs/ns3_packet_engine.argv"
        argv_path.write_text(
            argv_path.read_text(encoding="utf-8") + "--durationMs=1\n",
            encoding="utf-8",
        )
        ready_path = self.run_dir / "raw/state/ns3-engine.ready.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["config_sha256"] = "0" * 64
        write_json(ready_path, ready)
        _details, failures = self._validate()
        self.assertTrue(any("argv log differs" in item for item in failures), failures)

        # Restore argv so the independently checked ready hash is reached.
        argv_path.write_text("\n".join(self.engine_argv) + "\n", encoding="utf-8")
        _details, failures = self._validate()
        self.assertTrue(any("readiness" in item for item in failures), failures)

    def test_sampled_process_cmdline_hash_and_restart_are_rejected(self) -> None:
        self._write_runtime_sample("0" * 64)
        _details, failures = self._validate()
        self.assertTrue(any("cmdline/executable differs" in item for item in failures))

        expected = self._expected_cmdline_sha256()
        first = {
            "event": "warmup_resource_sample",
            "processes": {
                "processes": [
                    {
                        "pid": 101,
                        "start_ticks": 202,
                        "pgid": 101,
                        "role": "ns3_packet_engine",
                        "executable_path": self.binary_path,
                        "executable_sha256": self.binary_sha256,
                        "cmdline_sha256": expected,
                    }
                ]
            },
        }
        second = copy.deepcopy(first)
        second["event"] = "measurement_resource_sample"
        second["processes"]["processes"][0]["pid"] = 102
        path = self.run_dir / "logs/m4_runtime_events.jsonl"
        path.write_text(
            json.dumps(first, sort_keys=True)
            + "\n"
            + json.dumps(second, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        _details, failures = self._validate()
        self.assertTrue(any("identity changed" in item for item in failures), failures)

    def test_missing_live_process_samples_fail_closed_unless_explicitly_skipped(self) -> None:
        (self.run_dir / "logs/m4_runtime_events.jsonl").unlink()
        _details, failures = self._validate()
        self.assertTrue(any("sampled ns-3 process" in item for item in failures))
        details, failures = self._validate(inspect_runtime_processes=False)
        self.assertEqual(failures, [])
        self.assertFalse(details["sampled_engine_process"]["inspected"])


if __name__ == "__main__":
    unittest.main()
