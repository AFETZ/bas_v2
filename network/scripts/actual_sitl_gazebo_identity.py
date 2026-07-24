#!/usr/bin/env python3
"""Resolve the one live Gazebo server owned by an actual-SITL launch.

Gazebo Harmonic starts a short-lived shell launcher and a Ruby server, both of
which include ``gz sim`` in their command line.  The server is the process that
must be monitored for the M4 physical-flight proof; treating the pair as an
ambiguous generic ``gz sim`` match makes startup timing-dependent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ProcessRecord:
    """The bounded /proc facts needed for Gazebo lineage selection."""

    pid: int
    ppid: int
    pgid: int
    session_id: int
    start_ticks: int
    comm: str
    argv: tuple[str, ...]


def _has_gz_sim(argv: tuple[str, ...]) -> bool:
    """Accept the two exact argv forms emitted by the owned Harmonic server."""

    # Ruby normally sets its process title to one argv[0] value, ``gz sim``.
    # The shell launcher retains the conventional split executable/command
    # form.  Both remain constrained by Ruby identity, launch lineage, server
    # mode, and the resolved world below.
    if argv and Path(argv[0]).name == "gz sim":
        return True
    for index, value in enumerate(argv):
        if (
            Path(value).name == "gz"
            and index + 1 < len(argv)
            and argv[index + 1] == "sim"
        ):
            return True
    return False


def _is_descendant(record: ProcessRecord, records: dict[int, ProcessRecord], root: int) -> bool:
    current = record.ppid
    seen = {record.pid}
    while current not in seen:
        if current == root:
            return True
        seen.add(current)
        parent = records.get(current)
        if parent is None:
            return False
        current = parent.ppid
    return False


def select_gazebo_server(
    records: Iterable[ProcessRecord], *, flight_pid: int, world_path: str
) -> ProcessRecord | None:
    """Return one exact Ruby ``gz sim -s`` descendant, else fail closed."""

    snapshot = {record.pid: record for record in records}
    matches = []
    for record in snapshot.values():
        executable = Path(record.argv[0]).name.lower() if record.argv else ""
        if not (
            (record.comm.lower().startswith("ruby") or executable.startswith("ruby"))
            and _has_gz_sim(record.argv)
            and "-s" in record.argv
            and world_path in record.argv
            and _is_descendant(record, snapshot, flight_pid)
        ):
            continue
        matches.append(record)
    return matches[0] if len(matches) == 1 else None


def _read_record(entry: Path) -> ProcessRecord | None:
    try:
        raw_stat = (entry / "stat").read_text(encoding="utf-8")
        fields = raw_stat[raw_stat.rfind(")") + 2 :].split()
        if len(fields) <= 19:
            return None
        argv = tuple(
            value.decode(errors="replace")
            for value in (entry / "cmdline").read_bytes().split(b"\0")
            if value
        )
        return ProcessRecord(
            pid=int(entry.name),
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            session_id=int(fields[3]),
            start_ticks=int(fields[19]),
            comm=(entry / "comm").read_text(encoding="utf-8").strip(),
            argv=argv,
        )
    except (OSError, ValueError, IndexError):
        return None


def _snapshot() -> list[ProcessRecord]:
    return [
        record
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit() and (record := _read_record(entry)) is not None
    ]


def _diagnostic(
    records: Iterable[ProcessRecord], *, flight_pid: int, world_path: str
) -> str:
    candidates = []
    for record in records:
        if not _has_gz_sim(record.argv):
            continue
        candidates.append(
            {
                "argv": list(record.argv[:16]),
                "comm": record.comm,
                "matches_world": world_path in record.argv,
                "pgid": record.pgid,
                "pid": record.pid,
                "ppid": record.ppid,
                "session_id": record.session_id,
                "start_ticks": record.start_ticks,
            }
        )
    return json.dumps(
        {
            "candidate_count": len(candidates),
            "candidates": candidates[:16],
            "flight_pid": flight_pid,
            "world_path": world_path,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flight-pid", type=int, required=True)
    parser.add_argument("--world", required=True)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.flight_pid <= 1 or args.timeout_s <= 0.0:
        raise SystemExit("invalid Gazebo identity arguments")
    deadline = time.monotonic() + args.timeout_s
    records: list[ProcessRecord] = []
    while time.monotonic() < deadline:
        records = _snapshot()
        selected = select_gazebo_server(
            records, flight_pid=args.flight_pid, world_path=args.world
        )
        if selected is not None:
            print(f"{selected.pid}:{selected.start_ticks}")
            return 0
        time.sleep(0.2)
    print(
        "FAIL exact Gazebo server identity was not discovered: "
        + _diagnostic(records, flight_pid=args.flight_pid, world_path=args.world),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
