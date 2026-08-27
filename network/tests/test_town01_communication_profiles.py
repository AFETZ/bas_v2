"""Focused unit tests for bounded Town01 communication-profile orchestration."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.product import town01_communication_profiles as profiles


class FakeSocket:
    def __init__(self, datagrams: list[tuple[bytes, tuple[str, int]]]) -> None:
        self.datagrams = datagrams
        self.receive_calls = 0

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        self.receive_calls += 1
        if not self.datagrams:
            raise BlockingIOError
        return self.datagrams.pop(0)


class Town01CommunicationProfileTests(unittest.TestCase):
    def test_available_datagram_drain_stops_at_deadline(self) -> None:
        ready = FakeSocket(
            [
                (b"first", ("127.0.0.1", 1)),
                (b"second", ("127.0.0.1", 2)),
                (b"late", ("127.0.0.1", 3)),
            ]
        )
        with mock.patch.object(
            profiles.time, "monotonic_ns", side_effect=(0, 1, 2, 3, 5)
        ):
            observed = list(profiles.available_datagrams_until(ready, 5))

        self.assertEqual([item[0] for item in observed], [b"first", b"second"])
        self.assertEqual(ready.receive_calls, 2)

    def test_complete_jsonl_offset_excludes_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            complete = b'{"event":"one"}\n{"event":"two"}\n'
            path.write_bytes(complete + b'{"event":"part')

            self.assertEqual(profiles.complete_jsonl_end_offset(path), len(complete))

            path.write_bytes(complete)
            self.assertEqual(profiles.complete_jsonl_end_offset(path), len(complete))

            path.write_bytes(b'{"event":"part')
            self.assertEqual(profiles.complete_jsonl_end_offset(path), 0)

    def test_event_log_flush_wait_uses_configured_maximum_delay(self) -> None:
        with mock.patch.object(profiles.time, "sleep") as sleep:
            profiles.wait_for_event_log_flush(25, 2)

        sleep.assert_called_once_with(0.05)

    def test_engine_validation_binds_qos_ready_and_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            qos_path = root / "communication_qos.yaml"
            qos_path.write_text("profiles: {}\n", encoding="utf-8")
            qos_hash = hashlib.sha256(qos_path.read_bytes()).hexdigest()
            config_hash = "a" * 64
            epoch = 17
            engine_argv = [
                "--shapingEnabled=1",
                f"--configHash={config_hash}",
                f"--eventEpoch={epoch}",
            ]
            report = {
                "contract": profiles.ENGINE_CONTRACT,
                "config_sha256": config_hash,
                "resolved": {
                    "shaping_enabled": True,
                    "event_epoch": epoch,
                    "uav_count": 5,
                },
                "source_sha256": {str(qos_path): qos_hash},
                "engine_argv": engine_argv,
            }
            ready = {
                "status": "ready",
                "contract": profiles.ENGINE_CONTRACT,
                "config_sha256": config_hash,
                "event_epoch": epoch,
                "uav_count": 5,
                "pid": 1234,
            }
            (logs / "ns3_packet_engine_config.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (logs / "ns3_packet_engine.ready").write_text(
                json.dumps(ready), encoding="utf-8"
            )
            command = [
                "/tmp/ns3.40-ams-tap-packet-engine-default",
                *engine_argv,
            ]
            qos = {"profiles": {"nominal": {"shaping_enabled": True}}}

            with mock.patch.object(
                profiles, "live_engine_command_line", return_value=command
            ):
                observed = profiles.validate_engine_shaping_mode(
                    root, qos_path, qos, ("nominal",)
                )

            self.assertTrue(observed)

    def test_engine_validation_rejects_stale_qos_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            qos_path = root / "communication_qos.yaml"
            qos_path.write_text("current\n", encoding="utf-8")
            config_hash = "b" * 64
            report = {
                "contract": profiles.ENGINE_CONTRACT,
                "config_sha256": config_hash,
                "resolved": {
                    "shaping_enabled": True,
                    "event_epoch": 23,
                    "uav_count": 5,
                },
                "source_sha256": {str(qos_path): "0" * 64},
                "engine_argv": [f"--configHash={config_hash}"],
            }
            ready = {
                "status": "ready",
                "contract": profiles.ENGINE_CONTRACT,
                "config_sha256": config_hash,
                "event_epoch": 23,
                "uav_count": 5,
                "pid": 1234,
            }
            (logs / "ns3_packet_engine_config.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            (logs / "ns3_packet_engine.ready").write_text(
                json.dumps(ready), encoding="utf-8"
            )
            qos = {"profiles": {"nominal": {"shaping_enabled": True}}}

            with self.assertRaisesRegex(profiles.ProfileError, "QoS source SHA-256"):
                profiles.validate_engine_shaping_mode(
                    root, qos_path, qos, ("nominal",)
                )

    def test_engine_validation_rejects_ready_or_process_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            qos_path = root / "communication_qos.yaml"
            qos_path.write_text("current\n", encoding="utf-8")
            qos_hash = hashlib.sha256(qos_path.read_bytes()).hexdigest()
            config_hash = "c" * 64
            engine_argv = [f"--configHash={config_hash}", "--eventEpoch=29"]
            report = {
                "contract": profiles.ENGINE_CONTRACT,
                "config_sha256": config_hash,
                "resolved": {
                    "shaping_enabled": True,
                    "event_epoch": 29,
                    "uav_count": 5,
                },
                "source_sha256": {str(qos_path): qos_hash},
                "engine_argv": engine_argv,
            }
            ready = {
                "status": "ready",
                "contract": profiles.ENGINE_CONTRACT,
                "config_sha256": config_hash,
                "event_epoch": 29,
                "uav_count": 5,
                "pid": 1234,
            }
            report_path = logs / "ns3_packet_engine_config.json"
            ready_path = logs / "ns3_packet_engine.ready"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            qos = {"profiles": {"nominal": {"shaping_enabled": True}}}

            stale_ready = {**ready, "event_epoch": 30}
            ready_path.write_text(json.dumps(stale_ready), encoding="utf-8")
            with self.assertRaisesRegex(profiles.ProfileError, "readiness does not match"):
                profiles.validate_engine_shaping_mode(
                    root, qos_path, qos, ("nominal",)
                )

            ready_path.write_text(json.dumps(ready), encoding="utf-8")
            wrong_process = [
                "/tmp/ns3.40-ams-tap-packet-engine-default",
                *reversed(engine_argv),
            ]
            with mock.patch.object(
                profiles,
                "live_engine_command_line",
                return_value=wrong_process,
            ), self.assertRaisesRegex(profiles.ProfileError, "reported engine argv"):
                profiles.validate_engine_shaping_mode(
                    root, qos_path, qos, ("nominal",)
                )


if __name__ == "__main__":
    unittest.main()
