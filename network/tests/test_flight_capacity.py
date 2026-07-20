#!/usr/bin/env python3
"""Adversarial tests for the five-UAV flight capacity prerequisite."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from network.scripts.collect_flight_capacity import (
    EventWriter,
    load_profile as load_collector_profile,
    resource_sample_due,
)
from network.scripts.validate_flight_capacity import (
    EVENT_CONTRACT,
    OBSERVATION_CONTRACT,
    ROOT_DIR,
    canonical,
    derive_rtf_windows,
    interpolate_sim_ns,
    load_events,
    load_profile as load_validator_profile,
    sha256_file,
    validate_run,
)
from network.validation.qualification_identity import (
    qualification_consumption,
    qualification_content_vector,
)


IMAGE = "sha256:" + "1" * 64
RUNTIME_ID = "capacity-runtime-fixture"


class CapacityFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name) / "capacity_fixture"
        (self.run_dir / "logs").mkdir(parents=True)
        (self.run_dir / "metrics").mkdir(parents=True)
        self.start_ns = 32_000_000_000
        self.end_ns = self.start_ns + 300_000_000_000
        self.events: list[tuple[int, str, dict[str, Any]]] = []
        self._build()

    def close(self) -> None:
        self.temp.cleanup()

    def add(self, host_ns: int, event: str, **fields: Any) -> None:
        self.events.append((host_ns, event, fields))

    @staticmethod
    def process_sample() -> dict[str, Any]:
        counts = {
            "arducopter": 5,
            "mavproxy": 5,
            "micro_ros_agent": 5,
            "gazebo_server": 1,
        }
        return {
            "process_group": 1234,
            "counts": counts,
            "required_counts": counts,
            "roles_exact": True,
            "processes": [{"pid": index + 1} for index in range(16)],
            "process_count": 16,
            "total_cpu_time_s": 1.0,
            "total_rss_bytes": 4096,
        }

    @staticmethod
    def gpu_sample() -> dict[str, Any]:
        return {
            "available": True,
            "exit_code": 0,
            "stderr": "",
            "gpus": [
                {
                    "uuid": "GPU-fixture",
                    "name": "fixture",
                    "driver_version": "580.159.03",
                    "memory_total_mib": 8192,
                    "memory_used_mib": 100,
                    "utilization_percent": 10,
                    "temperature_c": 40,
                }
            ],
        }

    @staticmethod
    def cgroup_sample() -> dict[str, Any]:
        return {
            "paths": {"unified": "/fixture"},
            "cpu_max": "max 100000",
            "cpu_weight": "100",
            "cpuset_cpus_effective": "0-15",
            "memory_current": 1024,
            "memory_max": "max",
            "memory_swap_max": "max",
            "pids_current": 20,
            "pids_max": "max",
            "cpu_stat": {"usage_usec": 1},
        }

    def _build(self) -> None:
        profile_path = ROOT_DIR / "network/config/flight_capacity_profile.json"
        scenario_path = ROOT_DIR / "network/config/scenario_5uav.yaml"
        plan_path = ROOT_DIR / "doc/network_radio_integration_plan_v3.md"
        lock_path = ROOT_DIR / "network/config/dependency_lock.yaml"
        qualification_vector = qualification_content_vector(ROOT_DIR)
        qualification_record = qualification_consumption(
            qualification_vector, "flight_capacity_prerequisite"
        )
        provenance = {
            "schema_version": 2,
            "run_id": self.run_dir.name,
            "git_dirty": False,
            "git_status": [],
            "acceptance_eligible": True,
            "acceptance_blockers": [],
            "qualification_consumption": qualification_record,
            "qualification_content_vector": qualification_vector,
            "container_image": {
                "digest": IMAGE,
                "digest_source": "docker_image_inspect_host",
            },
            "inherited_m0_qualification": {"available": True},
            "config_hashes": {
                "network/config/flight_capacity_profile.json": sha256_file(profile_path),
                "network/config/scenario_5uav.yaml": sha256_file(scenario_path),
                "doc/network_radio_integration_plan_v3.md": sha256_file(plan_path),
                "network/config/dependency_lock.yaml": sha256_file(lock_path),
            },
        }
        provenance_path = self.run_dir / "metrics/provenance.json"
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

        self.add(
            100_000_000,
            "collector_start",
            profile_sha256=sha256_file(profile_path),
            provenance_sha256=sha256_file(provenance_path),
            static_runtime_identity={
                "cpu_model": "fixture cpu",
                "cpu_count": 16,
                "cpu_online": "0-15",
                "cpu_possible": "0-15",
                "governors": {"cpu0": "performance"},
                "meminfo_sha256": "4" * 64,
                "kernel": {"sysname": "Linux"},
                "clocksource": "tsc",
                "available_clocksources": "tsc hpet",
                "capabilities": {},
                "cgroup": self.cgroup_sample(),
                "gpu": self.gpu_sample(),
                "mitsuba_variant": "cuda_ad_mono_polarized",
            },
        )
        self.add(500_000_000, "readiness_complete", stable_since_monotonic_ns=1)
        self.add(1_000_000_000, "warmup_start", target_duration_ns=30_000_000_000)
        self.add(31_000_000_000, "warmup_end", target_monotonic_ns=31_000_000_000)
        self.add(
            self.start_ns,
            "measurement_start",
            target_end_monotonic_ns=self.end_ns,
        )
        self.add(
            self.end_ns,
            "measurement_end",
            target_monotonic_ns=self.end_ns,
            clock_bracket_observed=True,
        )

        for index in range(3002):
            host_ns = self.start_ns - 100_000_000 + index * 100_000_000
            self.add(host_ns + 1, "clock_sample", sim_time_ns=host_ns)
        for index in range(300):
            self.add(
                self.start_ns + index * 1_000_000_000 + 10_000_000,
                "measurement_resource_sample",
                sample_index=index,
                process_group=self.process_sample(),
                cgroup=self.cgroup_sample(),
                gpu=self.gpu_sample(),
            )
        for uav_index in range(1, 6):
            for sequence in range(1500):
                host_ns = (
                    self.start_ns
                    + sequence * 200_000_000
                    + 20_000_000
                    + uav_index * 1000
                )
                self.add(
                    host_ns,
                    "odometry_sample",
                    uav=f"uav{uav_index}",
                    sequence=sequence + 1,
                    sim_stamp_ns=host_ns,
                )
        self.write_events()
        self.write_observation()

    def write_events(self) -> None:
        ordered = sorted(self.events, key=lambda item: item[0])
        path = self.run_dir / "logs/flight_capacity_events.jsonl"
        with path.open("wb") as output:
            for index, (host_ns, event, fields) in enumerate(ordered):
                record = {
                    "schema_version": 1,
                    "contract": EVENT_CONTRACT,
                    "event_index": index,
                    "event": event,
                    "run_id": self.run_dir.name,
                    "runtime_id": RUNTIME_ID,
                    "host_monotonic_ns": host_ns,
                    "host_realtime_ns": host_ns + 1_000_000_000_000,
                    **fields,
                }
                output.write(canonical(record) + b"\n")

    def write_observation(self) -> None:
        events_path = self.run_dir / "logs/flight_capacity_events.jsonl"
        event_count = len(events_path.read_bytes().splitlines())
        observation = {
            "schema_version": 1,
            "contract": OBSERVATION_CONTRACT,
            "run_id": self.run_dir.name,
            "runtime_id": RUNTIME_ID,
            "completed": True,
            "failures": [],
            "profile_path": "network/config/flight_capacity_profile.json",
            "profile_sha256": sha256_file(
                ROOT_DIR / "network/config/flight_capacity_profile.json"
            ),
            "provenance_sha256": sha256_file(
                self.run_dir / "metrics/provenance.json"
            ),
            "event_log_path": "logs/flight_capacity_events.jsonl",
            "event_log_sha256": sha256_file(events_path),
            "event_count": event_count,
            "clock_sample_count": 3002,
            "resource_sample_count": 300,
            "warmup_start_monotonic_ns": 1_000_000_000,
            "warmup_end_monotonic_ns": 31_000_000_000,
            "measurement_start_monotonic_ns": self.start_ns,
            "measurement_end_monotonic_ns": self.end_ns,
        }
        (self.run_dir / "metrics/flight_capacity_observation.json").write_text(
            json.dumps(observation), encoding="utf-8"
        )


class FlightCapacityTests(unittest.TestCase):
    @staticmethod
    def validate_fixture(fixture: CapacityFixture) -> dict[str, Any]:
        passed_gate = {
            "status": "passed",
            "proof": "fixture",
            "details": {"failures": []},
        }
        with mock.patch(
            "network.scripts.validate_flight_capacity.runtime_inputs_status",
            return_value=passed_gate,
        ), mock.patch(
            "network.scripts.validate_flight_capacity.scene_status",
            return_value=passed_gate,
        ):
            return validate_run(ROOT_DIR, fixture.run_dir)

    def test_interpolation_and_exact_300_windows(self) -> None:
        samples = [(index * 100_000_000, index * 100_000_000) for index in range(3002)]
        values = derive_rtf_windows(
            samples,
            50_000_000,
            window_count=300,
            window_ns=1_000_000_000,
            maximum_gap_ns=250_000_000,
        )
        self.assertEqual(len(values), 300)
        self.assertTrue(all(value == 1.0 for value in values))

    def test_collector_and_validator_share_the_committed_profile_contract(self) -> None:
        profile_path = ROOT_DIR / "network/config/flight_capacity_profile.json"
        collector_profile = load_collector_profile(profile_path)
        validator_profile = load_validator_profile(profile_path)
        self.assertEqual(collector_profile, validator_profile)
        self.assertEqual(collector_profile["clock_topic"], "/uav1/clock")
        self.assertTrue(resource_sample_due(299, 300, 299))
        self.assertFalse(resource_sample_due(300, 300, 300))

        with tempfile.TemporaryDirectory() as temp:
            event_path = Path(temp) / "events.jsonl"
            writer = EventWriter(event_path, run_id="fixture", runtime_id=RUNTIME_ID)
            writer.emit("fixture", observed_monotonic_ns=123)
            writer.close()
            event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(event["host_monotonic_ns"], 123)

        result = subprocess.run(
            [
                "/usr/bin/python3.10",
                "network/scripts/validate_flight_capacity.py",
                "--help",
            ],
            cwd=ROOT_DIR,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C",
                "HOME": "/nonexistent",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_interpolation_rejects_unbracketed_or_sparse_clock(self) -> None:
        with self.assertRaisesRegex(ValueError, "bracket"):
            interpolate_sim_ns([(10, 10), (20, 20)], 5, maximum_gap_ns=20)
        with self.assertRaisesRegex(ValueError, "gap"):
            interpolate_sim_ns([(10, 10), (40, 40)], 20, maximum_gap_ns=20)

    def test_complete_fixture_passes(self) -> None:
        fixture = CapacityFixture()
        self.addCleanup(fixture.close)
        result = self.validate_fixture(fixture)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["metrics"]["window_count"], 300)
        self.assertEqual(result["metrics"]["passing_window_count"], 300)

        # Matching forged node hashes in both producer-owned records must not
        # pass: the validator independently reconstructs the committed vector.
        provenance_path = fixture.run_dir / "metrics/provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        forged_hash = "f" * 64
        provenance["qualification_content_vector"]["node_hashes"]["Q1"] = forged_hash
        provenance["qualification_consumption"]["consumed_node_sha256"][
            "Q1"
        ] = forged_hash
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        fixture.write_observation()
        result = self.validate_fixture(fixture)
        self.assertFalse(result["passed"], result)
        self.assertFalse(result["gates"]["identity"]["passed"])

    def test_missing_uav_measurement_fails(self) -> None:
        fixture = CapacityFixture()
        self.addCleanup(fixture.close)
        fixture.events = [
            item
            for item in fixture.events
            if not (item[1] == "odometry_sample" and item[2].get("uav") == "uav5")
        ]
        fixture.write_events()
        fixture.write_observation()
        result = self.validate_fixture(fixture)
        self.assertFalse(result["passed"], result)
        self.assertFalse(result["gates"]["five_uav_continuity"]["passed"])

    def test_resource_gap_or_missing_role_fails(self) -> None:
        fixture = CapacityFixture()
        self.addCleanup(fixture.close)
        for _host, event, fields in fixture.events:
            if event == "measurement_resource_sample" and fields["sample_index"] == 100:
                fields["process_group"]["counts"]["arducopter"] = 4
                fields["process_group"]["roles_exact"] = False
                break
        fixture.write_events()
        fixture.write_observation()
        result = self.validate_fixture(fixture)
        self.assertFalse(result["passed"], result)
        self.assertFalse(result["gates"]["resources"]["passed"])

    def test_noncanonical_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_bytes(
                b'{"schema_version":1,"schema_version":1,"contract":"x"}\n'
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_events(path, run_id="x", runtime_id="runtime-123")

    def test_producer_pass_cannot_hide_bad_rtf(self) -> None:
        fixture = CapacityFixture()
        self.addCleanup(fixture.close)
        modified: list[tuple[int, str, dict[str, Any]]] = []
        for host, event, fields in fixture.events:
            if event == "clock_sample" and fixture.start_ns <= host < fixture.start_ns + 20_000_000_000:
                fields = dict(fields)
                fields["sim_time_ns"] = fixture.start_ns + int(
                    (fields["sim_time_ns"] - fixture.start_ns) * 0.5
                )
            modified.append((host, event, fields))
        fixture.events = modified
        fixture.write_events()
        fixture.write_observation()
        result = self.validate_fixture(fixture)
        self.assertFalse(result["passed"], result)
        self.assertFalse(result["gates"]["realtime_factor"]["passed"])


if __name__ == "__main__":
    unittest.main()
