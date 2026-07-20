#!/usr/bin/env python3
"""Append-only lifecycle evidence monitor for the M2 vertical slice.

The monitor deliberately has no networking, namespace, or process-control
privilege requirement.  It observes predeclared process identities through
``/proc`` and records topology/queue-input file identities at a fixed cadence.
It is an evidence producer: an identity mismatch is recorded in every sample,
not silently repaired and not converted into a producer-side PASS/FAIL claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MONITOR_SCHEMA = "ams.m2.monitor/v1"
SCHEMA_VERSION = 1
DEFAULT_SAMPLE_PERIOD_S = 0.5
# A persistent M2 collector is a first-class identity, not an anonymous
# background shell.  Keep each capture point separately observable; the ns-3
# epoch itself is recorded by its own lifecycle journal because it is expected
# to disappear during the stopped interval and acquire a new PID on recovery.
ROLE_NAMES = (
    "launch",
    "sitl",
    "mavproxy",
    "adapter",
    "gcs_endpoint",
    "ns3",
    "capture",
    "capture_tail",
    "capture_gcs",
    "capture_ns3_external_gcs",
    "capture_ns3_external_uav",
    "capture_uav",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class MonitorError(RuntimeError):
    """The monitor cannot produce a safe, append-only raw record."""


@dataclass(frozen=True)
class ProcessReference:
    role: str
    pid: int
    start_ticks: int
    cmdline_sha256: str

    def as_record(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_ticks": self.start_ticks,
            "cmdline_sha256": self.cmdline_sha256,
        }


@dataclass(frozen=True)
class FileInput:
    name: str
    path: Path
    declared_sha256: str | None

    def as_record(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "declared_sha256": self.declared_sha256,
        }


@dataclass(frozen=True)
class MonitorConfig:
    run_id: str
    runtime_id: str
    run_nonce: str
    output: Path
    roles: dict[str, ProcessReference]
    topology: FileInput | None
    queue_inputs: dict[str, FileInput]
    sample_period_s: float = DEFAULT_SAMPLE_PERIOD_S
    duration_s: float | None = None
    stop_file: Path | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"cannot serialize monitor record: {exc}") from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise MonitorError("short write while appending monitor JSONL")
        offset += written


class AppendOnlyJsonl:
    """Create one output file and fsync every complete JSONL record."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        runtime_id: str,
        run_nonce: str,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self._descriptor = os.open(path, flags, 0o640)
        except OSError as exc:
            raise MonitorError(f"cannot exclusively create monitor output {path}: {exc}") from exc
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            self._directory_descriptor = os.open(path.parent, directory_flags)
            os.fsync(self._directory_descriptor)
        except OSError as exc:
            os.close(self._descriptor)
            raise MonitorError(f"cannot fsync monitor output directory {path.parent}: {exc}") from exc
        self._run_id = run_id
        self._runtime_id = runtime_id
        self._run_nonce = run_nonce
        self._sequence = 0

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if not isinstance(event, str) or not event:
            raise MonitorError("monitor event must be a nonempty string")
        self._sequence += 1
        record = {
            "schema": MONITOR_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "run_id": self._run_id,
            "runtime_id": self._runtime_id,
            "run_nonce": self._run_nonce,
            "event_seq": self._sequence,
            "event": event,
            "wall_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        _write_all(self._descriptor, canonical_json(record))
        try:
            os.fsync(self._descriptor)
        except OSError as exc:
            raise MonitorError(f"cannot fsync monitor output: {exc}") from exc
        return record

    def close(self) -> None:
        descriptor, directory_descriptor = self._descriptor, self._directory_descriptor
        self._descriptor = -1
        self._directory_descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)

    def __enter__(self) -> "AppendOnlyJsonl":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def _parse_reference(value: str) -> tuple[int, int, str]:
    pieces = value.split(":")
    if len(pieces) != 3:
        raise argparse.ArgumentTypeError(
            "process reference must be PID:START_TICKS:CMDLINE_SHA256"
        )
    try:
        pid = int(pieces[0])
        start_ticks = int(pieces[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "process reference PID and START_TICKS must be integers"
        ) from exc
    command_hash = pieces[2]
    if pid <= 1 or start_ticks <= 0:
        raise argparse.ArgumentTypeError("process reference PID and START_TICKS must be positive")
    if SHA256_RE.fullmatch(command_hash) is None:
        raise argparse.ArgumentTypeError("process reference CMDLINE_SHA256 must be lowercase SHA-256")
    return pid, start_ticks, command_hash


def parse_role_reference(value: str) -> ProcessReference:
    role, separator, reference = value.partition("=")
    if not separator or role not in ROLE_NAMES:
        raise argparse.ArgumentTypeError(
            f"role reference must be one of {ROLE_NAMES} followed by =PID:START_TICKS:CMDLINE_SHA256"
        )
    pid, start_ticks, command_hash = _parse_reference(reference)
    return ProcessReference(role, pid, start_ticks, command_hash)


def _parse_named_value(value: str, *, argument: str) -> tuple[str, str]:
    name, separator, raw_value = value.partition("=")
    if not separator or NAME_RE.fullmatch(name) is None or not raw_value:
        raise argparse.ArgumentTypeError(f"{argument} must be NAME=VALUE")
    return name, raw_value


def parse_queue_input(value: str) -> tuple[str, Path]:
    name, raw_path = _parse_named_value(value, argument="--queue-input")
    return name, Path(raw_path)


def parse_named_sha256(value: str) -> tuple[str, str]:
    name, digest = _parse_named_value(value, argument="--queue-input-sha256")
    if SHA256_RE.fullmatch(digest) is None:
        raise argparse.ArgumentTypeError("--queue-input-sha256 must use lowercase SHA-256")
    return name, digest


def read_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    try:
        return int(fields[19])  # proc stat field 22; state begins at index zero.
    except (IndexError, ValueError):
        return None


def read_cmdline_sha256(pid: int) -> str | None:
    try:
        payload = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    return hashlib.sha256(payload).hexdigest()


def sample_process(reference: ProcessReference) -> dict[str, Any]:
    proc_path = Path(f"/proc/{reference.pid}")
    pid_present = proc_path.exists()
    observed_start_ticks = read_start_ticks(reference.pid)
    observed_cmdline_sha256 = read_cmdline_sha256(reference.pid)
    mismatches: list[str] = []
    if not pid_present:
        mismatches.append("pid_missing")
    if observed_start_ticks != reference.start_ticks:
        mismatches.append("start_ticks_mismatch")
    if observed_cmdline_sha256 != reference.cmdline_sha256:
        mismatches.append("cmdline_sha256_mismatch")
    identity_match = not mismatches
    return {
        "expected_pid": reference.pid,
        "expected_start_ticks": reference.start_ticks,
        "expected_cmdline_sha256": reference.cmdline_sha256,
        "pid_present": pid_present,
        # ``alive`` means that the exact expected process identity is alive,
        # not merely that this numeric PID happens to be occupied.
        "alive": identity_match,
        "identity_match": identity_match,
        "start_ticks": observed_start_ticks,
        "cmdline_sha256": observed_cmdline_sha256,
        "mismatches": mismatches,
    }


def observe_file(input_file: FileInput) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(input_file.path),
        "declared_sha256": input_file.declared_sha256,
        "exists": False,
        "regular": False,
        "size_bytes": None,
        "sha256": None,
        "matches_declared": None,
        "error": None,
    }
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(input_file.path, flags)
    except FileNotFoundError:
        record["error"] = "missing"
        return record
    except OSError as exc:
        record["error"] = f"open_failed:{exc.errno}"
        return record
    try:
        status = os.fstat(descriptor)
        record["exists"] = True
        if not stat.S_ISREG(status.st_mode):
            record["error"] = "not_regular"
            return record
        record["regular"] = True
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        record["size_bytes"] = size
        record["sha256"] = digest.hexdigest()
        if input_file.declared_sha256 is not None:
            record["matches_declared"] = record["sha256"] == input_file.declared_sha256
        return record
    except OSError as exc:
        record["error"] = f"read_failed:{exc.errno}"
        return record
    finally:
        os.close(descriptor)


def collect_sample(config: MonitorConfig) -> dict[str, Any]:
    sample_started_monotonic_ns = time.monotonic_ns()
    roles = {role: sample_process(config.roles[role]) for role in sorted(config.roles)}
    topology = observe_file(config.topology) if config.topology is not None else None
    queue_inputs = {
        name: observe_file(config.queue_inputs[name]) for name in sorted(config.queue_inputs)
    }
    sample_completed_monotonic_ns = time.monotonic_ns()
    return {
        "sample_started_monotonic_ns": sample_started_monotonic_ns,
        "sample_completed_monotonic_ns": sample_completed_monotonic_ns,
        "roles": roles,
        "all_roles_alive": all(record["alive"] for record in roles.values()),
        "topology": topology,
        "queue_inputs": queue_inputs,
    }


def _monitor_start_fields(config: MonitorConfig) -> dict[str, Any]:
    return {
        "sample_period_s": config.sample_period_s,
        "duration_s": config.duration_s,
        "stop_file": None if config.stop_file is None else str(config.stop_file),
        "roles": {role: config.roles[role].as_record() for role in sorted(config.roles)},
        "topology": None if config.topology is None else config.topology.as_record(),
        "queue_inputs": {
            name: config.queue_inputs[name].as_record()
            for name in sorted(config.queue_inputs)
        },
    }


def run_monitor(config: MonitorConfig) -> int:
    """Record samples until a signal, stop file, or requested duration ends it."""

    stop_reason: str | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_reason
        if stop_reason is None:
            stop_reason = f"signal:{signal.Signals(signum).name.lower()}"

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    started = time.monotonic()
    sample_count = 0
    all_roles_alive = True
    try:
        with AppendOnlyJsonl(
            config.output,
            run_id=config.run_id,
            runtime_id=config.runtime_id,
            run_nonce=config.run_nonce,
        ) as writer:
            writer.emit("monitor_start", **_monitor_start_fields(config))
            while True:
                sample = collect_sample(config)
                sample_count += 1
                all_roles_alive = all_roles_alive and bool(sample["all_roles_alive"])
                writer.emit("sample", **sample)

                if stop_reason is not None:
                    break
                if config.stop_file is not None and config.stop_file.exists():
                    stop_reason = "stop_file"
                    break
                elapsed = time.monotonic() - started
                if config.duration_s is not None and elapsed >= config.duration_s:
                    stop_reason = "duration"
                    break
                wait_s = config.sample_period_s
                if config.duration_s is not None:
                    wait_s = min(wait_s, max(0.0, config.duration_s - elapsed))
                if wait_s > 0:
                    time.sleep(wait_s)
            writer.emit(
                "monitor_stop",
                reason=stop_reason or "completed",
                sample_count=sample_count,
                all_roles_alive=all_roles_alive,
            )
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


def _finite_positive(value: str, *, argument: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{argument} must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(f"{argument} must be finite and positive")
    return parsed


def _nonempty_identifier(value: str, *, argument: str) -> str:
    if not value or len(value) > 256 or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError(f"{argument} must be a nonempty whitespace-free identifier")
    return value


def parse_args(argv: list[str] | None = None) -> MonitorConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--role", action="append", type=parse_role_reference, required=True)
    parser.add_argument("--topology-file", type=Path)
    parser.add_argument("--topology-sha256")
    parser.add_argument("--queue-input", action="append", type=parse_queue_input, default=[])
    parser.add_argument("--queue-input-sha256", action="append", type=parse_named_sha256, default=[])
    parser.add_argument("--sample-period-s", default=DEFAULT_SAMPLE_PERIOD_S, type=float)
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args(argv)

    for field in ("run_id", "runtime_id", "run_nonce"):
        try:
            _nonempty_identifier(getattr(args, field), argument=f"--{field.replace('_', '-')}")
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    if not math.isfinite(args.sample_period_s) or args.sample_period_s <= 0:
        parser.error("--sample-period-s must be finite and positive")
    if args.duration_s is not None and (
        not math.isfinite(args.duration_s) or args.duration_s <= 0
    ):
        parser.error("--duration-s must be finite and positive")
    if (args.topology_file is None) != (args.topology_sha256 is None):
        parser.error("--topology-file and --topology-sha256 must be supplied together")
    if args.topology_sha256 is not None and SHA256_RE.fullmatch(args.topology_sha256) is None:
        parser.error("--topology-sha256 must be lowercase SHA-256")

    roles: dict[str, ProcessReference] = {}
    for reference in args.role:
        if reference.role in roles:
            parser.error(f"duplicate --role {reference.role!r}")
        roles[reference.role] = reference

    queue_paths: dict[str, Path] = {}
    for name, path in args.queue_input:
        if name in queue_paths:
            parser.error(f"duplicate --queue-input {name!r}")
        queue_paths[name] = path
    queue_hashes: dict[str, str] = {}
    for name, digest in args.queue_input_sha256:
        if name in queue_hashes:
            parser.error(f"duplicate --queue-input-sha256 {name!r}")
        queue_hashes[name] = digest
    unknown_hashes = sorted(set(queue_hashes) - set(queue_paths))
    if unknown_hashes:
        parser.error(f"--queue-input-sha256 has no matching --queue-input: {unknown_hashes}")

    topology = (
        None
        if args.topology_file is None
        else FileInput("topology", args.topology_file, args.topology_sha256)
    )
    queue_inputs = {
        name: FileInput(name, path, queue_hashes.get(name))
        for name, path in queue_paths.items()
    }
    return MonitorConfig(
        run_id=args.run_id,
        runtime_id=args.runtime_id,
        run_nonce=args.run_nonce,
        output=args.output,
        roles=roles,
        topology=topology,
        queue_inputs=queue_inputs,
        sample_period_s=args.sample_period_s,
        duration_s=args.duration_s,
        stop_file=args.stop_file,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return run_monitor(parse_args(argv))
    except (MonitorError, OSError, ValueError) as exc:
        print(f"FAIL M2 lifecycle monitor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
