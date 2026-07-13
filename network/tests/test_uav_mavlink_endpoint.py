from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "network" / "bridge" / "uav_mavlink_endpoint.py"


def reserve_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class UavMavlinkEndpointTests(unittest.TestCase):
    def test_bidirectional_forwarding_and_exact_pid_shutdown(self) -> None:
        radio_port = reserve_udp_port()
        tail_port = reserve_udp_port()
        gcs_port = reserve_udp_port()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            event_log = temp / "adapter.jsonl"
            ready_file = temp / "adapter.ready"
            gcs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tail_peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            gcs.settimeout(3.0)
            tail_peer.settimeout(3.0)
            gcs.bind(("127.0.0.1", gcs_port))
            tail_peer.bind(("127.0.0.1", 0))

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(ADAPTER),
                    "--radio-bind",
                    f"127.0.0.1:{radio_port}",
                    "--tail-bind",
                    f"127.0.0.1:{tail_port}",
                    "--gcs",
                    f"127.0.0.1:{gcs_port}",
                    "--tail-peer-host",
                    "127.0.0.1",
                    "--event-log",
                    str(event_log),
                    "--ready-file",
                    str(ready_file),
                    "--run-id",
                    "adapter-unit-run",
                    "--runtime-id",
                    "adapter-unit-runtime",
                    "--run-nonce",
                    "adapter-unit-nonce",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 3.0
                while not ready_file.exists() and time.monotonic() < deadline:
                    self.assertIsNone(process.poll())
                    time.sleep(0.02)
                self.assertTrue(ready_file.exists())
                self.assertEqual(int(ready_file.read_text().strip()), process.pid)

                telemetry = b"telemetry-with-binary-\x00\xff"
                command = b"command-with-binary-\x00\xfe"
                tail_peer.sendto(telemetry, ("127.0.0.1", tail_port))
                self.assertEqual(gcs.recvfrom(65535)[0], telemetry)
                gcs.sendto(command, ("127.0.0.1", radio_port))
                self.assertEqual(tail_peer.recvfrom(65535)[0], command)

                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=3.0), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3.0)
                gcs.close()
                tail_peer.close()

            events = [json.loads(line) for line in event_log.read_text().splitlines()]
            self.assertEqual(events[0]["event"], "adapter_start")
            self.assertEqual(events[0]["pid"], process.pid)
            self.assertTrue(all(event["run_id"] == "adapter-unit-run" for event in events))
            self.assertTrue(
                all(event["runtime_id"] == "adapter-unit-runtime" for event in events)
            )
            self.assertTrue(
                all(event["run_nonce"] == "adapter-unit-nonce" for event in events)
            )
            forwards = [event for event in events if event["event"] == "forward"]
            self.assertEqual(
                [event["direction"] for event in forwards],
                ["tail_to_gcs", "gcs_to_tail"],
            )
            self.assertEqual(events[-1]["event"], "adapter_stop")
            self.assertEqual(events[-1]["counters"]["tail_to_gcs"], 1)
            self.assertEqual(events[-1]["counters"]["gcs_to_tail"], 1)
            self.assertEqual(events[-1]["counters"]["dropped_unexpected_peer"], 0)


if __name__ == "__main__":
    unittest.main()
