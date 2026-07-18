#!/usr/bin/env python3
"""Opaque UAV-side UDP adapter for the M2 namespace/TapBridge path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.bridge.opaque_udp_relay import ByteOpaqueUdpRelay, RelayError  # noqa: E402


def parse_endpoint(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError(f"invalid HOST:PORT endpoint: {value!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid endpoint port: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"endpoint port is outside 1..65535: {value!r}")
    return host, port


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JsonlLog:
    def __init__(self, path: Path, *, run_id: str, runtime_id: str, run_nonce: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("x", encoding="utf-8")
        self._run_id = run_id
        self._runtime_id = runtime_id
        self._run_nonce = run_nonce
        self._event_seq = 0

    def emit(self, event: str, **fields: Any) -> None:
        self._event_seq += 1
        record = {
            "schema_version": 2,
            "run_id": self._run_id,
            "runtime_id": self._runtime_id,
            "run_nonce": self._run_nonce,
            "event_seq": self._event_seq,
            "event": event,
            "wall_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        self._handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radio-bind", type=parse_endpoint, default="10.71.1.10:14601")
    parser.add_argument("--tail-bind", type=parse_endpoint, default="10.72.1.2:14560")
    parser.add_argument("--gcs", type=parse_endpoint, default="10.71.0.10:14600")
    parser.add_argument("--tail-peer-host", default="10.72.1.1")
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    args = parser.parse_args(argv)

    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    event_log = JsonlLog(
        args.event_log,
        run_id=args.run_id,
        runtime_id=args.runtime_id,
        run_nonce=args.run_nonce,
    )
    selector = selectors.DefaultSelector()
    radio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tail = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (radio, tail):
        sock.setblocking(False)
    try:
        radio.bind(args.radio_bind)
        tail.bind(args.tail_bind)
        selector.register(radio, selectors.EVENT_READ, "radio")
        selector.register(tail, selectors.EVENT_READ, "tail")
    except OSError as exc:
        event_log.emit("adapter_bind_failed", error=str(exc))
        event_log.close()
        print(f"FAIL adapter bind: {exc}", file=sys.stderr)
        return 2

    relay = ByteOpaqueUdpRelay(
        radio,
        tail,
        args.gcs,
        tail_peer_host=args.tail_peer_host,
        strict_tail_peer=False,
        forwarding_enabled=True,
    )
    counters = {
        "gcs_to_tail": 0,
        "tail_to_gcs": 0,
        "dropped_no_peer": 0,
        "dropped_unexpected_peer": 0,
    }
    event_log.emit(
        "adapter_start",
        pid=os.getpid(),
        radio_bind=args.radio_bind,
        tail_bind=args.tail_bind,
        tail_peer_host=args.tail_peer_host,
        gcs=args.gcs,
    )
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    # The namespace launcher is normally a sudo/ip-netns wrapper.  Its `$!` is
    # therefore not necessarily the adapter process that owns the sockets.
    # Publish the real PID so lifecycle code can signal this process exactly
    # and then reap the wrapper without resorting to a broad pkill.
    with args.ready_file.open("x", encoding="utf-8") as ready:
        ready.write(f"{os.getpid()}\n")

    try:
        while not stop:
            for key, _mask in selector.select(timeout=0.5):
                sock = key.fileobj
                try:
                    payload, peer = sock.recvfrom(65535)
                except BlockingIOError:
                    continue
                digest = hashlib.sha256(payload).hexdigest()
                if key.data == "tail":
                    decision = relay.relay_tail(payload, peer)
                    if decision.action != "forwarded":
                        counters["dropped_unexpected_peer"] += 1
                        event_log.emit(
                            "drop",
                            direction="tail_to_gcs",
                            reason=decision.reason,
                            source=peer,
                            bytes=len(payload),
                            sha256=digest,
                        )
                        continue
                    counters["tail_to_gcs"] += 1
                    event_log.emit(
                        "forward",
                        direction="tail_to_gcs",
                        source=peer,
                        destination=args.gcs,
                        bytes=len(payload),
                        sha256=digest,
                    )
                else:
                    decision = relay.relay_radio(payload, peer)
                    if decision.reason == "unexpected_gcs_peer":
                        counters["dropped_unexpected_peer"] += 1
                        event_log.emit(
                            "drop",
                            direction="gcs_to_tail",
                            reason=decision.reason,
                            source=peer,
                            bytes=len(payload),
                            sha256=digest,
                        )
                    elif decision.reason == "mavproxy_peer_unknown":
                        counters["dropped_no_peer"] += 1
                        event_log.emit(
                            "drop",
                            direction="gcs_to_tail",
                            reason=decision.reason,
                            source=peer,
                            bytes=len(payload),
                            sha256=digest,
                        )
                    elif decision.action == "forwarded":
                        counters["gcs_to_tail"] += 1
                        event_log.emit(
                            "forward",
                            direction="gcs_to_tail",
                            source=peer,
                            destination=decision.destination,
                            bytes=len(payload),
                            sha256=digest,
                        )
                    else:
                        raise RelayError(
                            f"unexpected shared relay decision: {decision}"
                        )
    except (RelayError, OSError) as exc:
        event_log.emit(
            "adapter_failed_closed",
            reason=str(exc),
            counters=counters,
        )
        print(f"FAIL adapter relay: {exc}", file=sys.stderr)
        return 2
    finally:
        event_log.emit("adapter_stop", counters=counters)
        selector.close()
        radio.close()
        tail.close()
        event_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
