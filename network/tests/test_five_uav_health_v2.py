#!/usr/bin/env python3
"""Focused M1 health-evidence tests."""

from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.tests.collect_five_uav_health import (  # noqa: E402
    launch_log_findings,
    rate_hz,
    readiness_status,
    run_process_monitor,
    selected_measurement_duration,
)
from network.validation.evidence import five_uav_health_status  # noqa: E402


def health_record(run_id: str, duration_s: float = 300.0) -> dict:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "component_only": True,
        "packet_path_eligible": False,
        "observed_duration_s": duration_s,
        "minimum_duration_s": 300.0,
        "errors": [],
        "uavs": [
            {
                "name": f"uav{index}",
                "system_id": index,
                "dds_udp_port": 2018 + index,
                "gazebo_model": True,
                "sitl_healthy": True,
                "heartbeat": True,
                "heartbeat_rate_hz": 1.0,
                "heartbeat_age_s": 0.2,
                "odometry_fresh": True,
                "odometry_rate_hz": 20.0,
                "odometry_age_s": 0.05,
            }
            for index in range(1, 6)
        ],
    }


def write_complete_health_evidence(run_dir: Path) -> None:
    source_hash = "b" * 64
    runtime_id = "runtime-complete"
    start_ns = 1_000_000_000
    duration_ns = 300_000_000_000
    events = [
        {
            "event": "health_probe_start",
            "monotonic_ns": start_ns,
            "run_id": run_dir.name,
            "runtime_id": runtime_id,
            "source_hash": source_hash,
        }
    ]
    events.append(
        {
            "event": "measurement_start",
            "monotonic_ns": start_ns,
            "measurement_started_monotonic_ns": start_ns,
            "run_id": run_dir.name,
            "runtime_id": runtime_id,
            "source_hash": source_hash,
        }
    )
    uavs = []
    for index in range(1, 6):
        name = f"uav{index}"
        for sample in range(1501):
            offset_ns = sample * 200_000_000
            events.append(
                {
                    "event": "odometry",
                    "monotonic_ns": start_ns + offset_ns,
                    "uav": name,
                    "stamp_ns": offset_ns,
                    "valid": True,
                }
            )
        for sample in range(301):
            offset_ns = sample * 1_000_000_000
            events.append(
                {
                    "event": "heartbeat",
                    "monotonic_ns": start_ns + offset_ns,
                    "system_id": index,
                    "sim_time_ns": offset_ns,
                }
            )
        for sample in range(2):
            events.append(
                {
                    "event": "mavlink_global_position",
                    "monotonic_ns": start_ns + sample * duration_ns,
                    "system_id": index,
                    "lat_deg": 46.607213,
                    "lon_deg": 14.278461,
                    "relative_alt_m": 0.0,
                    "home_distance_m": 0.0,
                }
            )
        uavs.append(
            {
                "name": name,
                "system_id": index,
                "dds_udp_port": 2018 + index,
                "gazebo_model": True,
                "sitl_healthy": True,
                "heartbeat": True,
                "heartbeat_count": 301,
                "heartbeat_rate_hz": 1.0,
                "heartbeat_time_basis": "odometry_sim_stamp",
                "heartbeat_age_s": 0.0,
                "heartbeat_max_gap_s": 1.0,
                "heartbeat_sim_age_s": 0.0,
                "heartbeat_sim_start_delay_s": 0.0,
                "heartbeat_sim_max_gap_s": 1.0,
                "odometry_fresh": True,
                "odometry_count": 1501,
                "odometry_rate_hz": 5.0,
                "odometry_age_s": 0.0,
                "odometry_start_delay_s": 0.0,
                "odometry_max_gap_s": 0.2,
                "odometry_realtime_factor": 1.0,
                "odometry_invalid_samples": 0,
                "odometry_nonadvancing_stamps": 0,
                "odometry_max_displacement_m": 0.0,
                "odometry_max_speed_mps": 0.0,
                "mavlink_pose": True,
                "mavlink_position_count": 2,
                "mavlink_valid_home_position_count": 2,
            }
        )
    for sample in range(61):
        events.append(
            {
                "event": "process_sample",
                "monotonic_ns": start_ns + sample * 4_900_000_000,
                "counts": {
                    "arducopter": 5,
                    "mavproxy": 5,
                    "micro_ros_agent": 5,
                    "gazebo": 1,
                },
            }
        )
    events.append(
        {
            "event": "health_probe_complete",
            "monotonic_ns": start_ns + duration_ns,
            "measurement_ended_monotonic_ns": start_ns + duration_ns,
            "observed_duration_s": 300.0,
            "passed": True,
            "errors": [],
        }
    )
    priority = {"health_probe_start": 0, "health_probe_complete": 2}
    events.sort(key=lambda event: (event["monotonic_ns"], priority.get(event["event"], 1)))
    previous_clock = -1
    for event_seq, event in enumerate(events, start=1):
        event["monotonic_ns"] = max(event["monotonic_ns"], previous_clock + 1)
        previous_clock = event["monotonic_ns"]
        event.update(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "event_seq": event_seq,
                "wall_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    next(
        event for event in events if event["event"] == "health_probe_complete"
    )["measurement_ended_monotonic_ns"] = previous_clock
    raw = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "metrics").mkdir(parents=True)
    (run_dir / "logs/five_uav_health_events.jsonl").write_text(raw, encoding="utf-8")
    (run_dir / "logs/five_uav_launch.log").write_text(
        "startup link 1 down\nmeasurement healthy\n", encoding="utf-8"
    )
    (run_dir / "metrics/provenance.json").write_text(
        json.dumps({"source_hash": source_hash}), encoding="utf-8"
    )
    summary = {
        "schema_version": 2,
        "run_id": run_dir.name,
        "runtime_id": runtime_id,
        "source_hash": source_hash,
        "component_only": True,
        "packet_path_eligible": False,
        "observed_duration_s": 300.0,
        "minimum_duration_s": 300.0,
        "launch_log": "logs/five_uav_launch.log",
        "launch_log_observation_offset": len("startup link 1 down\n"),
        "raw_event_log": "logs/five_uav_health_events.jsonl",
        "raw_event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "errors": [],
        "passed": True,
        "uavs": uavs,
        "process_health": {
            "samples": 61,
            "observed_minimums": {
                "arducopter": 5,
                "mavproxy": 5,
                "micro_ros_agent": 5,
                "gazebo": 1,
            },
        },
    }
    (run_dir / "metrics/five_uav_health.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )


class FiveUavHealthV2Tests(unittest.TestCase):
    def test_rate_uses_sample_intervals(self) -> None:
        self.assertEqual(
            rate_hz({"count": 6, "first_monotonic_s": 10.0, "last_monotonic_s": 12.5}),
            2.0,
        )
        self.assertEqual(rate_hz({"count": 1}), 0.0)

    def test_startup_link_down_is_excluded_but_runtime_link_down_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "launch.log"
            startup = b"Waiting for heartbeat\nlink 1 down\n"
            path.write_bytes(startup + b"healthy observation\n")
            self.assertEqual(launch_log_findings(path, len(startup)), [])
            path.write_bytes(startup + b"healthy observation\nlink 1 down\n")
            self.assertIn("link 1 down", launch_log_findings(path, len(startup)))

    def test_fatal_startup_error_is_never_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "launch.log"
            startup = b"process has died\n"
            path.write_bytes(startup + b"observation\n")
            self.assertIn("process has died", launch_log_findings(path, len(startup)))

    def test_readiness_requires_gps_for_every_uav(self) -> None:
        names = [f"uav{index}" for index in range(1, 6)]
        odometry = {name: {"count": 2} for name in names}
        heartbeats = {index: {"count": 1} for index in range(1, 6)}
        positions = {
            index: {"count": 2, "valid_home_position_count": 2}
            for index in range(1, 6)
        }
        ready, details = readiness_status(names, odometry, heartbeats, positions)
        self.assertTrue(ready)
        positions[5] = {"count": 0, "valid_home_position_count": 0}
        ready, details = readiness_status(names, odometry, heartbeats, positions)
        self.assertFalse(ready)
        self.assertEqual(details["mavlink_position_counts"]["5"], 0)

    def test_failed_readiness_is_a_fail_fast_measurement_contract(self) -> None:
        # The runtime collector selects a zero-length measurement when this
        # predicate remains false; it must not spend 300 seconds on a run that
        # is already ineligible at readiness.
        names = [f"uav{index}" for index in range(1, 6)]
        ready, _details = readiness_status(names, {}, {}, {})
        self.assertFalse(ready)
        self.assertEqual(selected_measurement_duration(ready, 300.0), 0.0)
        self.assertEqual(selected_measurement_duration(True, 300.0), 300.0)

    def test_process_monitor_stops_without_ros_executor_work(self) -> None:
        stop_event = threading.Event()
        samples = []
        emitted = []

        class FakeEventLog:
            def emit(self, event: str, **fields: object) -> None:
                emitted.append((event, fields))
                stop_event.set()

        run_process_monitor(
            42,
            time.monotonic(),
            stop_event,
            samples,
            FakeEventLog(),
            interval_s=0.01,
            sampler=lambda process_group: (
                {
                    "arducopter": 5,
                    "mavproxy": 5,
                    "micro_ros_agent": 5,
                    "gazebo": 1,
                },
                [{"pid": process_group}],
                None,
            ),
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(emitted[0][0], "process_sample")
        self.assertEqual(samples[0]["counts"]["arducopter"], 5)

    def test_short_health_run_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "short_run"
            (run_dir / "metrics").mkdir(parents=True)
            (run_dir / "metrics/five_uav_health.json").write_text(
                json.dumps(health_record(run_dir.name, duration_s=10.0)), encoding="utf-8"
            )
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("below 300", "\n".join(result["details"]["failures"]))

    def test_complete_numeric_health_record_can_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "complete_run"
            write_complete_health_evidence(run_dir)
            self.assertEqual(five_uav_health_status(run_dir)["status"], "passed")

    def test_summary_without_raw_events_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "summary_only"
            (run_dir / "metrics").mkdir(parents=True)
            record = health_record(run_dir.name)
            record.update({"runtime_id": "forged-runtime", "source_hash": "a" * 64})
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": "a" * 64}), encoding="utf-8"
            )
            (run_dir / "metrics/five_uav_health.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("raw evidence", "\n".join(result["details"]["failures"]))

    def test_mixed_identity_and_broken_event_sequence_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "mixed_raw"
            write_complete_health_evidence(run_dir)
            raw_path = run_dir / "logs/five_uav_health_events.jsonl"
            records = [json.loads(line) for line in raw_path.read_text().splitlines()]
            records[2]["runtime_id"] = "runtime-from-another-run"
            records[3]["event_seq"] = records[2]["event_seq"]
            raw = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
            raw_path.write_text(raw, encoding="utf-8")
            summary_path = run_dir / "metrics/five_uav_health.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["raw_event_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("runtime_id", failures)
            self.assertIn("event_seq", failures)

    def test_fatal_launch_marker_is_derived_from_log_not_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "fatal_log"
            write_complete_health_evidence(run_dir)
            with (run_dir / "logs/five_uav_launch.log").open("a", encoding="utf-8") as log:
                log.write("process has died despite forged errors=[]\n")

            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("process has died", "\n".join(result["details"]["failures"]))


if __name__ == "__main__":
    unittest.main()
