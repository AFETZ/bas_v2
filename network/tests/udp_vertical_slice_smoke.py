#!/usr/bin/env python3
"""Bidirectional opaque-UDP smoke roles for the M2 endpoint adapter."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path


def parse_endpoint(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator:
        raise argparse.ArgumentTypeError("endpoint must be HOST:PORT")
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("gcs", "tail"), required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=10.0)
    parser.add_argument("--gcs-bind", type=parse_endpoint, default="10.71.0.10:14600")
    parser.add_argument("--uav-radio", type=parse_endpoint, default="10.71.1.10:14601")
    parser.add_argument("--tail-bind", type=parse_endpoint, default="10.72.1.1:17000")
    parser.add_argument("--uav-tail", type=parse_endpoint, default="10.72.1.2:14560")
    args = parser.parse_args(argv)

    telemetry = f"telemetry:{args.nonce}".encode()
    command = f"command:{args.nonce}".encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout_s)
    started = time.monotonic_ns()
    try:
        if args.role == "gcs":
            sock.bind(args.gcs_bind)
            received, peer = sock.recvfrom(65535)
            if received != telemetry:
                raise RuntimeError(f"unexpected telemetry payload: {received!r}")
            sock.sendto(command, args.uav_radio)
            result = {"role": "gcs", "received": received.decode(), "peer": peer}
        else:
            sock.bind(args.tail_bind)
            sock.sendto(telemetry, args.uav_tail)
            received, peer = sock.recvfrom(65535)
            if received != command:
                raise RuntimeError(f"unexpected command payload: {received!r}")
            result = {"role": "tail", "received": received.decode(), "peer": peer}
    except Exception as exc:
        print(f"FAIL {args.role} UDP smoke: {exc}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    result["elapsed_ms"] = (time.monotonic_ns() - started) / 1_000_000
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
