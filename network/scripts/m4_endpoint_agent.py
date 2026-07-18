#!/usr/bin/env python3
"""M4 external endpoint agent with per-process CLOCK_MONOTONIC beacons."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.m3_external_matrix_probe import EndpointAgent, ProbeError
from network.bridge.runtime_clock_beacon import beacon


class M4EndpointAgent(EndpointAgent):
    """Reuse the proven M3 byte path while allowing several named M4 windows."""

    def execute_phase(self, command: dict, command_hash: str) -> None:
        window_id = command.get("window_id")
        if not isinstance(window_id, str) or not window_id or "/" in window_id:
            raise ProbeError("M4 command lacks a safe window_id")
        super().execute_phase(command, command_hash)
        generic = self.args.run_dir / f"raw/state/{self.endpoint}.{command['phase']}.done.json"
        specific = self.args.run_dir / f"raw/state/{self.endpoint}.{window_id}.done.json"
        if specific.exists():
            raise ProbeError(f"M4 window completion already exists: {specific}")
        os.replace(generic, specific)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--endpoint", choices=("gcs", "uav1", "uav2", "uav3", "uav4", "uav5"), required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--clock-socket", type=Path, required=True)
    args = parser.parse_args()
    args.run_dir = args.run_dir.resolve()
    args.matrix = args.matrix.resolve()
    try:
        full_nonce = bytes.fromhex(args.run_nonce)
    except ValueError as exc:
        raise ProbeError("M4 run nonce is not lowercase hexadecimal") from exc
    if len(full_nonce) != 32 or args.run_nonce != args.run_nonce.lower():
        raise ProbeError("M4 run nonce must be exactly 256 bits")
    args.transport_run_nonce = hashlib.sha256(full_nonce).hexdigest()[:32]
    stop = threading.Event()
    thread = threading.Thread(
        target=beacon,
        args=(
            args.clock_socket.resolve(),
            f"endpoint_{args.endpoint}",
            stop,
        ),
        daemon=True,
    )
    thread.start()
    agent: M4EndpointAgent | None = None
    try:
        agent = M4EndpointAgent(args)
        agent.run()
    finally:
        stop.set()
        thread.join(2.0)
        if agent is not None:
            agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
