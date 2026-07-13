#!/usr/bin/env python3
"""Regression tests for evidence-driven network/radio validation."""

from __future__ import annotations

import contextlib
import io
import json
import hashlib
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import (  # noqa: E402
    delivery_status,
    evaluate_run,
    inspect_class_pcaps,
    no_bypass_status,
    packet_provenance_status,
    pcap_stats,
)


def write_pcap(path: Path, frames: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for index, frame in enumerate(frames, start=1):
            handle.write(struct.pack("<IIII", index, 0, len(frame), len(frame)))
            handle.write(frame)


def arp_frame() -> bytes:
    ethernet = b"\xff" * 6 + b"\x00\x01\x02\x03\x04\x05" + struct.pack("!H", 0x0806)
    return ethernet + (b"\x00" * 28)


def udp_frame(src_port: int, dst_port: int, payload: bytes) -> bytes:
    ethernet = b"\x10\x11\x12\x13\x14\x15" + b"\x00\x01\x02\x03\x04\x05" + struct.pack("!H", 0x0800)
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
        b"\x0a\x47\x00\x0a",
        b"\x0a\x47\x01\x0a",
    )
    udp = struct.pack("!HHHH", src_port, dst_port, 8 + len(payload), 0)
    return ethernet + ipv4 + udp + payload


def invalid_false_positive_summary() -> dict:
    return {
        "run_id": "fixture",
        "duration_s": 5,
        "p0_passed": True,
        "customer_ready": True,
        "packets": {
            "control_tx": 10,
            "control_rx": 0,
            "payload_tx": 10,
            "payload_rx": 0,
            "additional_tx": 10,
            "additional_rx": 0,
        },
        "loss_rate": {"control": 1.0, "payload": 1.0, "additional_data": 1.0},
        "latency_ms": {
            "control_p50": None,
            "control_p95": None,
            "payload_p50": None,
            "payload_p95": None,
        },
        "validation": {
            "no_bypass": True,
            "packet_path": True,
            "sionna_affects_packets": True,
            "shared_medium": True,
            "priority": True,
            "jamming_effect": True,
        },
    }


def valid_delivery_summary() -> dict:
    return {
        "packets": {
            "control_tx": 10,
            "control_rx": 10,
            "payload_tx": 10,
            "payload_rx": 9,
            "additional_tx": 10,
            "additional_rx": 8,
        },
        "loss_rate": {"control": 0.0, "payload": 0.1, "additional_data": 0.2},
        "latency_ms": {
            "control_p50": 10.0,
            "control_p95": 20.0,
            "payload_p50": 30.0,
            "payload_p95": 60.0,
        },
    }


class ValidationV2Tests(unittest.TestCase):
    def test_v3_profile_policy_blocks_legacy_all_pass_customer_claim(self) -> None:
        from network.validation.validate_run import main as validate_main

        fake_gate = {"status": "passed", "proof": "forged legacy all-pass"}
        fake_result = {
            "schema_version": 2,
            "validation_engine": "network.validation.evidence",
            "run_id": "legacy_all_pass",
            "p0_passed": True,
            "customer_ready": True,
            "gates": {
                "p0": {"legacy": fake_gate},
                "p1": {"long_run": fake_gate},
                "p2": {},
            },
            "runtime_metrics": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "legacy_all_pass"
            run_dir.mkdir()
            output = io.StringIO()
            with patch(
                "network.validation.validate_run.evaluate_run",
                return_value=fake_result,
            ), contextlib.redirect_stdout(output):
                return_code = validate_main(["--run-dir", str(run_dir), "--no-write"])

        self.assertEqual(return_code, 1)
        self.assertIn("P0 passed: false", output.getvalue())

    def test_zero_rx_full_loss_and_null_latency_fail(self) -> None:
        result = delivery_status(invalid_false_positive_summary())
        self.assertFalse(result["passed"])
        text = "\n".join(result["failures"])
        self.assertIn("received packet count is not positive", text)
        self.assertIn("complete packet loss", text)
        self.assertIn("latency_ms.control_p95 is missing", text)

    def test_arp_only_pcap_fails_even_when_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            for traffic_class in ("control", "payload", "additional_data"):
                write_pcap(run_dir / "pcap" / f"{traffic_class}.pcap", [arp_frame()])
            result = inspect_class_pcaps(run_dir)
            self.assertFalse(result["passed"])
            self.assertTrue(all(item["total_packets"] == 1 for item in result["classes"].values()))
            self.assertTrue(all(item["data_packets"] == 0 for item in result["classes"].values()))

    def test_class_pcap_identical_to_generic_ns3_capture_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            frame = udp_frame(14600, 14601, b"nonce-packet")
            for traffic_class in ("control", "payload", "additional_data"):
                write_pcap(run_dir / "pcap" / f"{traffic_class}.pcap", [frame])
            write_pcap(run_dir / "pcap" / "ns3-p2mp-0-0.pcap", [frame])
            result = inspect_class_pcaps(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn("byte-identical", "\n".join(result["failures"]))

    def test_positive_udp_pcap_and_delivery_subchecks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            for index, traffic_class in enumerate(("control", "payload", "additional_data")):
                write_pcap(
                    run_dir / "pcap" / f"{traffic_class}.pcap",
                    [udp_frame(14600 + index * 100, 14601 + index * 100, f"nonce-{traffic_class}".encode())],
                )
            self.assertTrue(inspect_class_pcaps(run_dir)["passed"])
            self.assertTrue(delivery_status(valid_delivery_summary())["passed"])

    def test_synthesized_no_bypass_text_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "logs" / "no_bypass_active.log").write_text(
                "PASS ns-3 stopped: synthetic text\n", encoding="utf-8"
            )
            (run_dir / "logs" / "no_bypass.log").write_text(
                "NOTE full P0 no-bypass proof still requires active endpoints with ns-3 stopped inside the namespace/TAP topology.\n",
                encoding="utf-8",
            )
            result = no_bypass_status(run_dir)
            self.assertNotEqual(result["status"], "passed")
            self.assertIn("structured", result["proof"])

    def test_structured_on_stopped_recovery_no_bypass_can_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "metrics").mkdir(parents=True)
            source_hash = "a" * 64
            runtime_id = "runtime-positive"
            run_nonce = "nonce-positive"
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": source_hash}), encoding="utf-8"
            )
            (run_dir / "metrics/joint_runtime.json").write_text(
                json.dumps({"runtime_id": runtime_id}), encoding="utf-8"
            )
            sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
            stable_processes = {
                "gcs_endpoint": {"pid": 101, "start_ticks": 1001, "cmdline_sha256": sha("gcs")},
                "uav_endpoint": {"pid": 102, "start_ticks": 1002, "cmdline_sha256": sha("uav")},
                "sitl": {"pid": 103, "start_ticks": 1003, "cmdline_sha256": sha("sitl")},
            }
            events = [{"event": "experiment_start"}]
            for phase, attempts in (("ns3_on", 10), ("ns3_stopped", 5), ("ns3_recovered", 10)):
                events.append(
                    {
                        "event": "endpoint_health",
                        "phase": phase,
                        "all_live": True,
                        "stable_processes": stable_processes,
                    }
                )
                events.append(
                    {
                        "event": "ns3_state",
                        "phase": phase,
                        "running": phase != "ns3_stopped",
                        **(
                            {
                                "pid": 201 if phase == "ns3_on" else 202,
                                "start_ticks": 2001 if phase == "ns3_on" else 2002,
                                "cmdline_sha256": sha(phase),
                            }
                            if phase != "ns3_stopped"
                            else {}
                        ),
                    }
                )
                for attempt_number in range(1, attempts + 1):
                    request_hash = sha(f"{phase}-request-{attempt_number}")
                    nonce = f"{run_nonce}:{phase}:{attempt_number}"
                    request = {
                        "event": "command_attempt",
                        "phase": phase,
                        "attempt": attempt_number,
                        "nonce": nonce,
                        "marker_sha256": sha(f"{phase}-marker-{attempt_number}"),
                        "request_sha256": request_hash,
                        "mavlink_seq": attempt_number,
                        "target_system": 1,
                        "mavlink_command": 512,
                    }
                    events.append(request)
                    if phase == "ns3_stopped":
                        events.append(
                            {
                                "event": "command_timeout",
                                "phase": phase,
                                "attempt": attempt_number,
                                "request_sha256": request_hash,
                            }
                        )
                    else:
                        events.append(
                            {
                                "event": "command_ack",
                                "phase": phase,
                                "attempt": attempt_number,
                                "nonce": nonce,
                                "request_sha256": request_hash,
                                "request_mavlink_seq": attempt_number,
                                "packet_sha256": sha(f"{phase}-ack-{attempt_number}"),
                                "source_system": 1,
                                "mavlink_command": 512,
                                "mavlink_result": 0,
                            }
                        )
                if phase == "ns3_stopped":
                    events.append(
                        {
                            "event": "heartbeat_timeout",
                            "phase": phase,
                            "timed_out": True,
                            "timeout_s": 3.0,
                        }
                    )
                else:
                    events.append(
                        {
                            "event": "heartbeat",
                            "phase": phase,
                            "packet_sha256": sha(f"{phase}-heartbeat"),
                            "source_system": 1,
                        }
                    )
            events.append({"event": "experiment_complete", "errors": []})
            for sequence, event in enumerate(events, start=1):
                event.update(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": runtime_id,
                        "source_hash": source_hash,
                        "event_seq": sequence,
                        "monotonic_ns": sequence * 1_000_000,
                        "wall_utc": "2026-07-12T12:00:00Z",
                    }
                )
            raw = "".join(json.dumps(event) + "\n" for event in events)
            raw_path = run_dir / "logs/no_bypass_events.jsonl"
            raw_path.write_text(raw, encoding="utf-8")
            data = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "run_nonce": run_nonce,
                "raw_event_log": "logs/no_bypass_events.jsonl",
                "raw_event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
            (run_dir / "logs" / "no_bypass_active.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            self.assertEqual(no_bypass_status(run_dir)["status"], "passed")

    def test_command_result_booleans_cannot_pass_no_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "logs").mkdir()
            (run_dir / "metrics").mkdir()
            source_hash = "a" * 64
            runtime_id = "runtime-forged"
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": source_hash}), encoding="utf-8"
            )
            (run_dir / "metrics/joint_runtime.json").write_text(
                json.dumps({"runtime_id": runtime_id}), encoding="utf-8"
            )
            events = [
                {"event": "command_result", "phase": phase, "ack": phase != "ns3_stopped"}
                for phase, count in (("ns3_on", 10), ("ns3_stopped", 5), ("ns3_recovered", 10))
                for _ in range(count)
            ]
            raw = "".join(json.dumps(event) + "\n" for event in events)
            (run_dir / "logs/no_bypass_events.jsonl").write_text(raw, encoding="utf-8")
            (run_dir / "logs/no_bypass_active.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": runtime_id,
                        "source_hash": source_hash,
                        "run_nonce": "forged-nonce",
                        "raw_event_log": "logs/no_bypass_events.jsonl",
                        "raw_event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            result = no_bypass_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("decoded command attempts", "\n".join(result["details"]["failures"]))

    def test_packet_provenance_correlates_ack_without_claiming_ack_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "packet_provenance"
            for directory in ("logs", "metrics", "pcap"):
                (run_dir / directory).mkdir(parents=True, exist_ok=True)
            source_hash = "b" * 64
            runtime_id = "runtime-packet-positive"
            run_nonce = "packet-run-nonce"
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": source_hash}), encoding="utf-8"
            )
            (run_dir / "metrics/joint_runtime.json").write_text(
                json.dumps({"runtime_id": runtime_id}), encoding="utf-8"
            )
            packets = {
                "control_tx": 5,
                "control_rx": 5,
                "payload_tx": 5,
                "payload_rx": 5,
                "additional_tx": 5,
                "additional_rx": 5,
            }
            (run_dir / "metrics/runtime_summary.json").write_text(
                json.dumps({"packets": packets}), encoding="utf-8"
            )

            payloads: list[bytes] = []
            events: list[dict] = [{"event": "transaction_start"}]
            for target_system in range(1, 6):
                attempt = target_system
                attempt_nonce = f"{run_nonce}:uav{target_system}:{attempt}"
                marker_payload = f"STATUSTEXT:{attempt_nonce}".encode()
                command_payload = f"COMMAND:{target_system}:{attempt}".encode()
                ack_payload = f"COMMAND_ACK:{target_system}:{attempt}".encode()
                telemetry_payload = f"AUTOPILOT_VERSION:{target_system}:{attempt}".encode()
                marker_hash = hashlib.sha256(marker_payload).hexdigest()
                command_hash = hashlib.sha256(command_payload).hexdigest()
                ack_hash = hashlib.sha256(ack_payload).hexdigest()
                telemetry_hash = hashlib.sha256(telemetry_payload).hexdigest()
                payloads.extend((marker_payload, command_payload, ack_payload, telemetry_payload))
                events.extend(
                    [
                        {
                            "event": "command_attempt",
                            "attempt": attempt,
                            "nonce": attempt_nonce,
                            "marker_sha256": marker_hash,
                            "command_sha256": command_hash,
                            "mavlink_seq": attempt,
                            "target_system": target_system,
                            "mavlink_command": 512,
                        },
                        {
                            "event": "command_ack",
                            # COMMAND_ACK has no nonce field; correlation is by
                            # exact request hash/sequence/system/command.
                            "request_sha256": command_hash,
                            "request_mavlink_seq": attempt,
                            "packet_sha256": ack_hash,
                            "source_system": target_system,
                            "mavlink_command": 512,
                            "mavlink_result": 0,
                        },
                        {
                            "event": "telemetry",
                            "request_sha256": command_hash,
                            "request_mavlink_seq": attempt,
                            "packet_sha256": telemetry_hash,
                            "source_system": target_system,
                            "message_id": 148,
                        },
                    ]
                )
            for source_system in range(1, 6):
                heartbeat_payload = f"HEARTBEAT:{source_system}".encode()
                payloads.append(heartbeat_payload)
                events.append(
                    {
                        "event": "heartbeat",
                        "source_system": source_system,
                        "packet_sha256": hashlib.sha256(heartbeat_payload).hexdigest(),
                    }
                )
            events.append({"event": "transaction_complete", "errors": []})
            for sequence, event in enumerate(events, start=1):
                event.update(
                    {
                        "schema_version": 2,
                        "run_id": run_dir.name,
                        "runtime_id": runtime_id,
                        "source_hash": source_hash,
                        "event_seq": sequence,
                        "monotonic_ns": sequence * 1_000_000,
                        "wall_utc": "2026-07-12T12:00:00Z",
                    }
                )
            raw = "".join(json.dumps(event) + "\n" for event in events)
            (run_dir / "logs/mavlink_transactions.jsonl").write_text(raw, encoding="utf-8")

            capture_points = {}
            for point_index, point in enumerate(
                ("gcs_ingress", "ns3_ingress", "ns3_egress", "uav_egress"), start=1
            ):
                path = run_dir / f"pcap/{point}.pcap"
                point_payloads = payloads + [f"capture-point:{point_index}".encode()]
                write_pcap(
                    path,
                    [udp_frame(14600, 14601, payload) for payload in point_payloads],
                )
                capture_points[point] = {
                    "path": f"pcap/{point}.pcap",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }

            packet_record = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "run_nonce": run_nonce,
                "response_timeout_ms": 1000.0,
                "traffic_classes": {
                    "control": {"tx": 5, "rx": 5},
                    "payload": {"tx": 5, "rx": 5},
                    "additional_data": {"tx": 5, "rx": 5},
                },
                "capture_points": capture_points,
                "raw_mavlink_log": "logs/mavlink_transactions.jsonl",
                "raw_mavlink_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "mavlink": {
                    "command_attempts": 5,
                    "command_acks": 5,
                    "telemetry_messages": 5,
                    "heartbeats": 5,
                },
            }
            (run_dir / "metrics/packet_provenance.json").write_text(
                json.dumps(packet_record), encoding="utf-8"
            )

            result = packet_provenance_status(
                run_dir, {"failures": []}, {"failures": []}
            )
            self.assertEqual(result["status"], "passed", result)

            events[2]["request_sha256"] = "f" * 64
            forged_raw = "".join(json.dumps(event) + "\n" for event in events)
            (run_dir / "logs/mavlink_transactions.jsonl").write_text(
                forged_raw, encoding="utf-8"
            )
            packet_record["raw_mavlink_sha256"] = hashlib.sha256(forged_raw.encode()).hexdigest()
            (run_dir / "metrics/packet_provenance.json").write_text(
                json.dumps(packet_record), encoding="utf-8"
            )
            forged = packet_provenance_status(
                run_dir, {"failures": []}, {"failures": []}
            )
            self.assertEqual(forged["status"], "failed")
            self.assertIn("unknown command frame", "\n".join(forged["details"]["failures"]))

    def test_self_reported_true_flags_do_not_pass_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "forged"
            (run_dir / "metrics").mkdir(parents=True)
            (run_dir / "metrics" / "summary.json").write_text(
                json.dumps(invalid_false_positive_summary()), encoding="utf-8"
            )
            result = evaluate_run(run_dir)
            self.assertFalse(result["p0_passed"])
            self.assertNotEqual(result["gates"]["p0"]["priority"]["status"], "passed")
            self.assertNotEqual(result["gates"]["p0"]["jamming"]["status"], "passed")

    def test_postprocess_does_not_fabricate_pcaps_or_active_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "postprocess"
            for name in ("metrics", "logs", "pcap"):
                (run_dir / name).mkdir(parents=True, exist_ok=True)
            (run_dir / "metrics" / "summary.json").write_text(
                json.dumps(invalid_false_positive_summary()), encoding="utf-8"
            )
            (run_dir / "metrics" / "ns3_link_states.csv").write_text(
                "time_s,tx,rx,traffic_class,pathloss_db,rssi_dbm,sinr_db,js_db,service_tier_bps,per_input,link_state,stale,source\n",
                encoding="utf-8",
            )
            (run_dir / "logs" / "no_bypass.log").write_text(
                "NOTE full P0 no-bypass proof still requires active endpoints\n", encoding="utf-8"
            )
            (run_dir / "logs" / "bridge.jsonl").write_text("\n", encoding="utf-8")
            write_pcap(run_dir / "pcap" / "ns3-p2mp-0-0.pcap", [arp_frame()])
            subprocess.run(
                [sys.executable, str(ROOT_DIR / "network/scripts/postprocess_sim_2_4ghz_run.py"), "--run-dir", str(run_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse((run_dir / "pcap" / "control.pcap").exists())
            self.assertFalse((run_dir / "logs" / "no_bypass_active.log").exists())
            summary = json.loads((run_dir / "metrics" / "runtime_summary.json").read_text())
            self.assertFalse(summary["p0_passed"])
            self.assertFalse(summary["customer_ready"])
            self.assertNotIn("validation", summary)
            self.assertIn("producer_observations", summary)
            original_summary = json.loads((run_dir / "metrics" / "summary.json").read_text())
            self.assertTrue(original_summary["p0_passed"])

    def test_historical_false_positive_run_is_rejected_when_available(self) -> None:
        run_dir = ROOT_DIR / "runs/real_packet_loop_20260702T113341Z"
        if not run_dir.is_dir():
            self.skipTest("historical regression run is not present in this checkout")
        result = evaluate_run(run_dir)
        self.assertFalse(result["p0_passed"])
        self.assertEqual(result["gates"]["p0"]["three_traffic_classes"]["status"], "failed")
        self.assertNotEqual(result["gates"]["p0"]["no_bypass"]["status"], "passed")
        stats = pcap_stats(run_dir / "pcap/control.pcap")
        self.assertEqual(stats["udp_packets"], 0)


if __name__ == "__main__":
    unittest.main()
