#!/usr/bin/env python3
"""Append one fsync'd, identity-bound M2 lifecycle event.

The M2 runner is deliberately the only lifecycle producer.  This small helper
keeps its JSONL ordering and durable append semantics out of shell redirection:
each event has a contiguous sequence, the immutable run identity, and both
wall-clock and CLOCK_MONOTONIC timestamps.  Validators treat this file as raw
ordering evidence, never as a producer PASS flag.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "ams.m2.lifecycle/v1"
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def strict_load(line: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value {value!r}")

    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    value = json.loads(
        line,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )
    if not isinstance(value, dict):
        raise ValueError("event must be a JSON object")
    return value


def parse_field(value: str) -> tuple[str, Any]:
    key, separator, raw = value.partition("=")
    if not separator or FIELD_RE.fullmatch(key) is None:
        raise argparse.ArgumentTypeError("--field must be KEY=VALUE with a safe key")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("--field must contain a finite number")
    return key, parsed


def _last_sequence(
    handle: Any,
    *,
    run_id: str,
    runtime_id: str,
    run_nonce: str,
) -> int:
    handle.seek(0)
    last = 0
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            raise ValueError(f"blank lifecycle record at line {line_number}")
        record = strict_load(line)
        if record.get("schema") != SCHEMA:
            raise ValueError(f"lifecycle schema mismatch at line {line_number}")
        if (
            record.get("run_id"),
            record.get("runtime_id"),
            record.get("run_nonce"),
        ) != (run_id, runtime_id, run_nonce):
            raise ValueError(f"mixed lifecycle identity at line {line_number}")
        sequence = record.get("event_seq")
        if type(sequence) is not int or sequence != last + 1:
            raise ValueError(f"non-contiguous lifecycle event_seq at line {line_number}")
        last = sequence
    handle.seek(0, os.SEEK_END)
    return last


def append_event(
    output: Path,
    *,
    run_id: str,
    runtime_id: str,
    run_nonce: str,
    event: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    if EVENT_RE.fullmatch(event) is None:
        raise ValueError("event has an unsafe format")
    if any(key in {"schema", "run_id", "runtime_id", "run_nonce", "event_seq", "event", "wall_utc", "monotonic_ns"} for key in fields):
        raise ValueError("--field attempts to overwrite a reserved lifecycle key")
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(output, flags, 0o600)
    try:
        with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                last = _last_sequence(
                    handle,
                    run_id=run_id,
                    runtime_id=runtime_id,
                    run_nonce=run_nonce,
                )
                record = {
                    "schema": SCHEMA,
                    "run_id": run_id,
                    "runtime_id": runtime_id,
                    "run_nonce": run_nonce,
                    "event_seq": last + 1,
                    "event": event,
                    "wall_utc": utc_now(),
                    "monotonic_ns": time.monotonic_ns(),
                    **fields,
                }
                encoded = json.dumps(
                    record,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n"
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                return record
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--field", action="append", default=[], type=parse_field)
    args = parser.parse_args(argv)
    fields: dict[str, Any] = {}
    for key, value in args.field:
        if key in fields:
            parser.error(f"duplicate --field {key}")
        fields[key] = value
    args.fields = fields
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        record = append_event(
            args.output,
            run_id=args.run_id,
            runtime_id=args.runtime_id,
            run_nonce=args.run_nonce,
            event=args.event,
            fields=args.fields,
        )
    except (OSError, ValueError) as exc:
        print(f"FAIL M2 lifecycle event: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
