#!/usr/bin/env python3
"""Read five live ArduPilot parameter tables through the existing GCS UDP ports.

Run inside ams-gcs, outside the measurement window, with MAVProxy/other local
clients closed. GCSService returns telemetry to its most recent client per port;
this program does not create a second issuer, serial connection or radio path.
Protocol: https://mavlink.io/en/services/parameter.html
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import json
import math
import os
from pathlib import Path
import signal
import struct
import time


TYPES = {1: ("UINT8", "B"), 2: ("INT8", "b"), 3: ("UINT16", "H"),
         4: ("INT16", "h"), 5: ("UINT32", "I"), 6: ("INT32", "i"),
         7: ("UINT64", "Q"), 8: ("INT64", "q"), 9: ("REAL32", "f"),
         10: ("REAL64", "d")}
CSV_FIELDS = ["system_id", "component_id", "param_index", "param_count", "param_id",
              "param_type", "type_name", "encoding", "value_json", "wire_float_text",
              "wire_value_hex", "decoding_issue", "last_received_monotonic_ns"]


def value_wire_bytes(message) -> bytes:
    """Use the CRC-validated frame: pinned get_payload() assumes a v1 header."""
    frame = bytes(message.get_msgbuf())
    offset = 10 if frame[:1] == b"\xfd" else 6 if frame[:1] == b"\xfe" else None
    if offset is None or len(frame) < offset + 6 or frame[1] < 4:
        raise ValueError("unsupported/truncated PARAM_VALUE frame")
    return frame[offset:offset + 4]


def receive_logged(link):
    """Upstream nonblocking receiver with its raw/tlog writers enabled.

    Pinned mavudp.recv_msg overrides the common receiver without the logging
    calls. Use the inherited public mavfile implementation on this UDP object.
    """
    from pymavlink import mavutil
    return mavutil.mavfile.recv_msg(link)


def decode_value(raw: bytes, param_type: int, encoding: str) -> tuple[object, str | None]:
    """Decode the original four wire bytes, including bytewise NaN bit patterns."""
    if len(raw) != 4 or param_type not in TYPES:
        return None, "unsupported_type_or_truncated_wire_value"
    _, fmt = TYPES[param_type]
    if struct.calcsize(fmt) > 4:
        return None, "64_bit_type_cannot_be_recovered_from_four_wire_bytes"
    if encoding == "bytewise":
        value = struct.unpack("<" + fmt, raw[:struct.calcsize(fmt)])[0]
    else:
        value = struct.unpack("<f", raw)[0]
        if fmt != "f":
            if not math.isfinite(value) or not value.is_integer():
                return None, "nonintegral_or_nonfinite_c_cast_integer"
            value = int(value)
            try:
                struct.pack("<" + fmt, value)
            except struct.error:
                return None, "c_cast_value_outside_declared_integer_range"
    if isinstance(value, float) and not math.isfinite(value):
        return None, "nonfinite_parameter_value"
    return value, None


class ParameterTable:
    def __init__(self, system_id: int, component_id: int, encoding: str):
        self.system_id, self.component_id, self.encoding = system_id, component_id, encoding
        self.counts: list[int] = []
        self.by_index: dict[int, str] = {}
        self.parameters: dict[str, dict] = {}
        self.conflicts: list[dict] = []
        self.changes: list[dict] = []
        self.rejected = Counter()
        self.duplicates = 0

    @property
    def count(self):
        return self.counts[0] if self.counts else None

    def missing(self):
        return None if self.count is None else sorted(set(range(self.count)) - self.by_index.keys())

    def accept(self, message, now_ns: int) -> bool:
        if (message.get_srcSystem(), message.get_srcComponent()) != (self.system_id, self.component_id):
            self.rejected["foreign_system_or_component"] += 1
            return False
        if message.get_type() != "PARAM_VALUE":
            return False
        name = message.param_id
        try:
            name = (name.decode("ascii") if isinstance(name, bytes) else str(name)).split("\0", 1)[0]
            if not name or len(name.encode("ascii")) > 16:
                raise ValueError("invalid name")
        except (UnicodeError, ValueError):
            self.rejected["invalid_param_id"] += 1
            return False
        count, index, param_type = int(message.param_count), int(message.param_index), int(message.param_type)
        if not 1 <= count <= 65535:
            self.rejected["invalid_param_count"] += 1
            return False
        if count not in self.counts:
            self.counts.append(count)
        if len(self.counts) > 1:
            self.conflicts.append(dict(reason="param_count_changed", counts=list(self.counts), time_ns=now_ns))
        raw = value_wire_bytes(message)
        value, issue = decode_value(raw, param_type, self.encoding)
        previous = self.parameters.get(name)
        indexed = index != 65535
        if indexed and (index >= count or count != self.count):
            self.conflicts.append(dict(reason="invalid_index_or_different_count_generation", name=name, index=index, count=count))
            indexed = False
        if indexed:
            old_name = self.by_index.get(index)
            if old_name is not None and old_name != name:
                self.conflicts.append(dict(reason="index_name_changed", index=index, previous=old_name, current=name))
            if previous is not None and previous["param_index"] not in (None, index):
                self.conflicts.append(dict(reason="name_index_changed", name=name, previous=previous["param_index"], current=index))
            self.by_index[index] = name
        entry = dict(system_id=self.system_id, component_id=self.component_id,
                     param_index=index if indexed else (previous["param_index"] if previous else None),
                     param_count=count, param_id=name, param_type=param_type,
                     type_name=TYPES.get(param_type, ("UNKNOWN", ""))[0], encoding=self.encoding,
                     value_json=json.dumps(value, allow_nan=False),
                     wire_float_text=repr(struct.unpack("<f", raw)[0]) if len(raw) == 4 else None,
                     wire_value_hex=raw.hex(), decoding_issue=issue,
                     last_received_monotonic_ns=now_ns)
        if previous is not None:
            if previous["param_type"] != param_type:
                self.conflicts.append(dict(reason="param_type_changed", name=name, previous=previous["param_type"], current=param_type))
            if (previous["param_type"], previous["wire_value_hex"]) != (param_type, raw.hex()):
                self.changes.append(dict(param_id=name, before=previous, after=entry, received_monotonic_ns=now_ns))
            else:
                self.duplicates += 1
        self.parameters[name] = entry
        return True

    def report(self):
        missing = self.missing()
        indexed_complete = missing == [] and len(set(self.by_index.values())) == self.count
        issues = [row for row in self.parameters.values() if row["decoding_issue"]]
        unindexed = [row["param_id"] for row in self.parameters.values() if row["param_index"] is None]
        complete = indexed_complete and len(self.counts) == 1 and not self.conflicts and not unindexed and not issues and not any(self.rejected[k] for k in ("invalid_param_id", "invalid_param_count"))
        return dict(system_id=self.system_id, component_id=self.component_id,
                    status=("complete_indexed_readout_with_observed_changes" if self.changes else "complete_indexed_readout") if complete else "partial_or_unstable",
                    complete=complete, index_coverage_complete=indexed_complete,
                    stable_values_observed=not self.changes, unresolved_unindexed_parameters=unindexed,
                    param_counts_observed=self.counts, expected_count=self.count,
                    received_index_count=len(self.by_index), missing_indices=missing,
                    missing_indices_not_addressable_by_int16_read=None if missing is None else [i for i in missing if i > 32767],
                    conflicts=self.conflicts, value_changes_during_readout=self.changes,
                    decoding_issues=issues, rejected_messages=dict(self.rejected),
                    duplicate_values=self.duplicates,
                    parameters=sorted(self.parameters.values(), key=lambda row: row["param_id"]))


class Requests:
    """Bounded native parameter requests; never restart an active list stream."""
    def __init__(self, rate_hz, retry_s, retries, list_idle_s):
        self.period_ns = int(1e9 / rate_hz)
        self.retry_ns, self.idle_ns = int(retry_s * 1e9), int(list_idle_s * 1e9)
        self.retries, self.list_attempts = retries, 0
        self.last_list_ns = self.last_send_ns = None
        self.repair_started = False
        self.read_attempts: dict[int, tuple[int, int]] = {}

    def next(self, table: ParameterTable, now_ns: int, last_value_ns: int | None):
        if self.last_send_ns is not None and now_ns - self.last_send_ns < self.period_ns:
            return None
        if table.count is None:
            if self.list_attempts >= self.retries or (self.last_list_ns is not None and now_ns - self.last_list_ns < self.retry_ns):
                return None
            self.list_attempts += 1
            self.last_list_ns = self.last_send_ns = now_ns
            return ("list", None)
        if not self.repair_started:
            if last_value_ns is None or now_ns - last_value_ns < self.idle_ns:
                return None
            self.repair_started = True
        for index in sorted(table.missing(), key=lambda item: (self.read_attempts.get(item, (0, 0))[0], item)):
            attempts, previous_ns = self.read_attempts.get(index, (0, -self.retry_ns))
            if index <= 32767 and attempts < self.retries and now_ns - previous_ns >= self.retry_ns:
                self.read_attempts[index] = (attempts + 1, now_ns)
                self.last_send_ns = now_ns
                return ("read", index)
        return None


def event(stream, kind, **fields):
    stream.write(json.dumps(dict(event=kind, received_host_monotonic_ns=time.monotonic_ns(),
                                 unix_ns=time.time_ns(), **fields), allow_nan=False) + "\n")


def close_link(link):
    for attribute in ("logfile", "logfile_raw"):
        handle = getattr(link, attribute, None)
        if handle:
            handle.close()
    link.close()


def open_link(args, system_id):
    from pymavlink import mavutil
    endpoint = f"udpout:127.0.0.1:{args.port_base + system_id}"
    link = mavutil.mavlink_connection(endpoint, source_system=255, source_component=195,
                                     dialect="ardupilotmega")
    try:
        link.setup_logfile(str(args.output_dir / f"uav{system_id}.tlog"))
        link.setup_logfile_raw(str(args.output_dir / f"uav{system_id}.raw"))
    except Exception:
        close_link(link)
        raise
    return link


def capture_one(args, system_id, running, link):
    from pymavlink import mavutil
    endpoint = f"udpout:127.0.0.1:{args.port_base + system_id}"
    table = ParameterTable(system_id, args.component_id, args.encoding)
    requests = Requests(args.read_rate_hz, args.retry_s, args.retries, args.list_idle_s)
    started, last_value_ns, heartbeat_ns, reason = time.monotonic_ns(), None, None, "timeout"
    stream = (args.output_dir / f"uav{system_id}.jsonl").open("w", buffering=1)
    try:
        while running[0] and (time.monotonic_ns() - started) / 1e9 < args.timeout_s:
            now = time.monotonic_ns()
            if heartbeat_ns is None or now - heartbeat_ns >= 1_000_000_000:
                link.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
                heartbeat_ns = now
                event(stream, "heartbeat_sent", endpoint=endpoint)
            request = requests.next(table, now, last_value_ns)
            if request:
                kind, index = request
                message = (link.mav.param_request_list_encode(system_id, args.component_id) if kind == "list" else
                           link.mav.param_request_read_encode(system_id, args.component_id, b"", index))
                link.mav.send(message)
                event(stream, "request_sent", request_type=message.get_type(), target_system=system_id,
                      target_component=args.component_id, param_index=index, frame_hex=bytes(message.get_msgbuf()).hex())
            message = receive_logged(link)
            if message is not None:
                accepted = table.accept(message, time.monotonic_ns())
                if accepted:
                    last_value_ns = time.monotonic_ns()
                fields = dict(message_type=message.get_type(), source_system=message.get_srcSystem(),
                              source_component=message.get_srcComponent(), accepted_parameter=accepted,
                              frame_hex=bytes(message.get_msgbuf()).hex())
                if message.get_type() == "PARAM_VALUE":
                    fields.update(param_id=str(message.param_id), param_count=int(message.param_count),
                                  param_index=int(message.param_index), param_type=int(message.param_type),
                                  wire_value_hex=value_wire_bytes(message).hex())
                event(stream, "frame_received", **fields)
            else:
                time.sleep(.005)
            if (table.count is not None and len(table.by_index) == table.count
                    and last_value_ns is not None and time.monotonic_ns() - last_value_ns >= int(args.settle_s * 1e9)
                    and table.report()["complete"]):
                reason = "complete_after_settle"
                break
        if not running[0]:
            reason = "signal"
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        event(stream, "capture_error", reason=reason)
    finally:
        stream.close()
        close_link(link)
    return dict(**table.report(), endpoint=endpoint, stop_reason=reason,
                duration_wall_s=(time.monotonic_ns() - started) / 1e9,
                list_requests=requests.list_attempts,
                read_requests=sum(attempts for attempts, _ in requests.read_attempts.values()))


def capture_all(args, running):
    # Pinned pymavlink changes a global dialect during protocol auto-detection.
    # Its native v2 parser accepts FE and FD without those global switches.
    # Initialize every parser/socket sequentially, before any worker receives.
    os.environ["MAVLINK20"] = "1"
    from pymavlink import mavutil
    mavutil.set_dialect("ardupilotmega")
    links, failures = {}, {}
    for system_id in args.sysids:
        try:
            links[system_id] = open_link(args, system_id)
        except Exception as error:
            failures[system_id] = f"{type(error).__name__}: {error}"

    def attempt(system_id):
        empty = lambda reason: dict(**ParameterTable(system_id, args.component_id, args.encoding).report(),
                                    stop_reason=reason)
        if system_id in failures:
            return empty(failures[system_id])
        link = links[system_id]
        if not running[0]:
            close_link(link)
            return empty("not_attempted_after_signal")
        try:
            return capture_one(args, system_id, running, link)
        except Exception as error:
            close_link(link)
            return empty(f"{type(error).__name__}: {error}")

    # One socket, parser, parameter table and set of raw files per worker.
    # Queued components do not send requests until a worker is available.
    with ThreadPoolExecutor(max_workers=min(args.workers, len(args.sysids))) as pool:
        return list(pool.map(attempt, args.sysids))


def compare(before: dict, after: dict):
    """Absent observations in partial reads are never reported as removals."""
    changes = []
    old = {(c["system_id"], c["component_id"]): c for c in before["components"]}
    for current in after["components"]:
        key = current["system_id"], current["component_id"]
        previous = old.get(key)
        if previous is None:
            changes.append(dict(system_id=key[0], component_id=key[1], change="component_not_observed_before"))
            continue
        a, b = ({r["param_id"]: r for r in component["parameters"]} for component in (previous, current))
        for name in sorted(a.keys() | b.keys()):
            if name not in a:
                change = "added" if previous["complete"] else "not_observed_before"
            elif name not in b:
                change = "removed" if current["complete"] else "not_observed_after"
            elif any(a[name][field] != b[name][field] for field in ("wire_value_hex", "param_type", "param_index")):
                change = "value_type_or_index_changed"
            else:
                continue
            changes.append(dict(system_id=key[0], component_id=key[1], param_id=name, change=change,
                                before=a.get(name), after=b.get(name)))
    return changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--compare-before", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--sysids", default="1,2,3,4,5")
    parser.add_argument("--component-id", type=int, default=1)
    parser.add_argument("--workers", type=int, choices=range(1, 6), default=1,
                        help="Concurrent UAV readouts on separate GCS ports; default sequential")
    parser.add_argument("--port-base", type=int, default=14550)
    parser.add_argument("--timeout-s", type=float, default=90)
    parser.add_argument("--read-rate-hz", type=float, default=10)
    parser.add_argument("--retry-s", type=float, default=1)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--list-idle-s", type=float, default=2)
    parser.add_argument("--settle-s", type=float, default=1)
    parser.add_argument("--encoding", choices=("c-cast", "bytewise"), default="c-cast")
    args = parser.parse_args(argv)
    try:
        args.sysids = [int(value) for value in args.sysids.split(",")]
    except ValueError:
        parser.error("sysids must be comma-separated integers")
    if (not args.sysids or len(set(args.sysids)) != len(args.sysids) or not set(args.sysids) <= set(range(1, 6))
            or not 1 <= args.component_id <= 255 or not 1 <= args.port_base <= 65530
            or not 0 < args.read_rate_hz <= 20 or not 1 <= args.retries <= 10
            or any(not math.isfinite(value) or value <= 0 for value in
                   (args.timeout_s, args.read_rate_hz, args.retry_s, args.list_idle_s, args.settle_s))):
        parser.error("invalid IDs, ports, bounds or finite positive periods")
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    running = [True]
    def stop(*_):
        running[0] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    summary = dict(run_id=args.run_id, started_unix_ns=time.time_ns(),
                   started_host_monotonic_ns=time.monotonic_ns(), components=[],
                   settings={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                   limits=["Completeness means stable indexed identity/count coverage for the targeted SYSID/COMPID, not all possible components or an atomic snapshot. Observed value changes remain explicit and do not erase valid index coverage.",
                           "C-cast is the explicit pinned ArduPilot encoding; integer wire values may already have float32 precision loss. Original four value bytes are retained.",
                           "No PARAM_SET or flight-parameter modification is sent. Read-all streams are paced by upstream ArduPilot; at most workers component streams run concurrently, each with bounded read repairs.",
                           "MAVLink2 ardupilotmega parsers accept both FE and FD frames. All connections initialize sequentially before threaded I/O, preventing global dialect auto-switch races.",
                           "tlog/raw contain received frames/bytes via upstream pymavlink; JSONL also retains outgoing request bytes.",
                           "GCSService has one local UDP subscriber per UAV. MAVProxy and other clients must be closed during this capture."])
    summary["effective_workers"] = min(args.workers, len(args.sysids))
    summary["mavlink_parser_protocol"] = "2.0"
    summary["components"] = capture_all(args, running)
    summary.update(finished_unix_ns=time.time_ns(), finished_host_monotonic_ns=time.monotonic_ns())
    summary["status"] = "complete" if all(c["complete"] for c in summary["components"]) else "partial"
    if args.compare_before:
        try:
            before = json.loads((args.compare_before / "summary.json").read_text())
            if before.get("run_id") is None or before.get("run_id") != summary["run_id"]:
                raise ValueError("before/after requires the same explicit run_id")
            summary["before_after_changes"] = compare(before, summary)
            summary["comparison_status"] = "both_complete" if before["status"] == summary["status"] == "complete" else "partial_observations"
        except (OSError, ValueError, KeyError) as error:
            summary["comparison_status"] = "unavailable"
            summary["comparison_error"] = str(error)
        with (args.output_dir / "changes.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["system_id", "component_id", "param_id", "change", "before_json", "after_json"])
            writer.writeheader()
            for change in summary.get("before_after_changes", []):
                writer.writerow({**{key: change.get(key) for key in ("system_id", "component_id", "param_id", "change")},
                                 "before_json": json.dumps(change.get("before")), "after_json": json.dumps(change.get("after"))})
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    with (args.output_dir / "parameters.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for component in summary["components"]:
            writer.writerows(component["parameters"])
    print(json.dumps({"status": summary["status"], "output_dir": str(args.output_dir),
                      "components": [{k: c[k] for k in ("system_id", "component_id", "status", "expected_count", "received_index_count", "stop_reason")} for c in summary["components"]]}, indent=2))
    return 0 if summary["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
