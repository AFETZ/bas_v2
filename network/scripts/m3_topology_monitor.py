#!/usr/bin/env python3
"""Continuously preserve M3 namespace, firewall, socket, and process state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


SAMPLE_SCHEMA = "ams.m3.topology_sample/v1"
SUMMARY_CONTRACT = "ams.m3.topology_monitor_summary/v1"
COMMAND_CONTRACT = "ams.m3.topology_monitor_command/v1"
ACK_CONTRACT = "ams.m3.topology_monitor_ack/v1"
NAMESPACE_NAMES = (
    "container-root",
    "ams-ns3",
    "ams-gcs",
    "ams-uav1",
    "ams-uav2",
    "ams-uav3",
    "ams-uav4",
    "ams-uav5",
)
REQUIRED_COMMANDS = (
    "ip",
    "bridge",
    "ss",
    "nft",
    "iptables-save",
    "ip6tables-save",
)
SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class MonitorError(RuntimeError):
    """Continuous topology evidence cannot be trusted."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_exclusive(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        payload = canonical_json(value)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def strict_json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MonitorError(f"duplicate key {key!r} in {path}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot read command {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MonitorError(f"command is not an object: {path}")
    return value


def command_paths() -> dict[str, str]:
    result: dict[str, str] = {}
    for command in REQUIRED_COMMANDS:
        path = shutil.which(command)
        if path is None:
            raise MonitorError(f"required topology command is absent: {command}")
        # Keep the command symlink basename: Debian's iptables-save is a
        # multi-call executable whose operation is selected from argv[0].
        result[command] = str(Path(path).absolute())
    return result


def namespace_prefix(namespace: str, commands: dict[str, str]) -> list[str]:
    if namespace == "container-root":
        return []
    return [commands["ip"], "netns", "exec", namespace]


def run_command(
    namespace: str,
    argv: list[str],
    commands: dict[str, str],
    *,
    expect_json: bool = False,
) -> Any:
    completed = subprocess.run(
        [*namespace_prefix(namespace, commands), *argv],
        check=False,
        capture_output=True,
        timeout=5,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MonitorError(
            f"{namespace} command failed rc={completed.returncode}: {argv!r}: {stderr}"
        )
    try:
        text = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MonitorError(f"{namespace} command emitted non-UTF-8: {argv!r}") from exc
    if not expect_json:
        return text.splitlines()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MonitorError(
            f"{namespace} command emitted invalid JSON: {argv!r}: {exc}"
        ) from exc


def namespace_inode(namespace: str) -> int | None:
    path = (
        Path("/proc/self/ns/net")
        if namespace == "container-root"
        else Path("/run/netns") / namespace
    )
    try:
        return path.stat().st_ino
    except OSError:
        return None


def collect_namespace(namespace: str, commands: dict[str, str]) -> dict[str, Any]:
    inode = namespace_inode(namespace)
    if inode is None:
        return {
            "present": False,
            "namespace_inode": None,
            "links": [],
            "addresses": [],
            "routes_ipv4": [],
            "routes_ipv6": [],
            "rules_ipv4": [],
            "rules_ipv6": [],
            "neighbours_ipv4": [],
            "neighbours_ipv6": [],
            "bridge_links": [],
            "sockets": [],
            "nftables": {},
            "iptables_ipv4": [],
            "iptables_ipv6": [],
        }
    prefix = namespace_prefix(namespace, commands)
    del prefix
    return {
        "present": True,
        "namespace_inode": inode,
        "links": run_command(
            namespace,
            [commands["ip"], "-j", "-d", "link", "show"],
            commands,
            expect_json=True,
        ),
        "addresses": run_command(
            namespace,
            [commands["ip"], "-j", "address", "show"],
            commands,
            expect_json=True,
        ),
        "routes_ipv4": run_command(
            namespace,
            [commands["ip"], "-4", "-j", "route", "show", "table", "all"],
            commands,
            expect_json=True,
        ),
        "routes_ipv6": run_command(
            namespace,
            [commands["ip"], "-6", "-j", "route", "show", "table", "all"],
            commands,
            expect_json=True,
        ),
        "rules_ipv4": run_command(
            namespace,
            [commands["ip"], "-4", "-j", "rule", "show"],
            commands,
            expect_json=True,
        ),
        "rules_ipv6": run_command(
            namespace,
            [commands["ip"], "-6", "-j", "rule", "show"],
            commands,
            expect_json=True,
        ),
        "neighbours_ipv4": run_command(
            namespace,
            [commands["ip"], "-4", "-j", "neighbour", "show"],
            commands,
            expect_json=True,
        ),
        "neighbours_ipv6": run_command(
            namespace,
            [commands["ip"], "-6", "-j", "neighbour", "show"],
            commands,
            expect_json=True,
        ),
        "bridge_links": run_command(
            namespace,
            [commands["bridge"], "-j", "-d", "link", "show"],
            commands,
            expect_json=True,
        ),
        "sockets": sorted(
            run_command(
                namespace,
                [commands["ss"], "-H", "-n", "-a", "-u", "-t", "-p"],
                commands,
            )
        ),
        "nftables": run_command(
            namespace,
            [commands["nft"], "-j", "list", "ruleset"],
            commands,
            expect_json=True,
        ),
        "iptables_ipv4": run_command(
            namespace, [commands["iptables-save"], "-c"], commands
        ),
        "iptables_ipv6": run_command(
            namespace, [commands["ip6tables-save"], "-c"], commands
        ),
    }


class ExecutableHasher:
    def __init__(self) -> None:
        self.cache: dict[tuple[int, int, int, int], str] = {}

    def hash(self, path: Path) -> str:
        stat_result = path.stat()
        key = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        value = digest.hexdigest()
        self.cache[key] = value
        return value


def process_start_ticks(stat_payload: str) -> int:
    close = stat_payload.rfind(")")
    if close < 0:
        raise MonitorError("/proc PID stat lacks command terminator")
    fields = stat_payload[close + 2 :].split()
    if len(fields) <= 19:
        raise MonitorError("/proc PID stat is truncated")
    return int(fields[19])


def collect_processes(
    namespaces: dict[str, dict[str, Any]], hasher: ExecutableHasher
) -> list[dict[str, Any]]:
    inode_to_name = {
        value["namespace_inode"]: name
        for name, value in namespaces.items()
        if value["present"] is True
    }
    records: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            inode = (entry / "ns/net").stat().st_ino
            namespace = inode_to_name.get(inode)
            if namespace is None:
                continue
            start_ticks = process_start_ticks(
                (entry / "stat").read_text(encoding="utf-8")
            )
            cmdline = [
                part.decode("utf-8", errors="surrogateescape")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
            executable = Path(os.readlink(entry / "exe"))
            executable_hash = hasher.hash(entry / "exe")
            status = (entry / "status").read_text(encoding="utf-8").splitlines()
            cap_eff = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in status
                    if line.startswith("CapEff:")
                ),
                "unavailable",
            )
            cgroup = (entry / "cgroup").read_text(encoding="utf-8").splitlines()
        except (
            FileNotFoundError,
            PermissionError,
            ProcessLookupError,
            OSError,
            ValueError,
        ):
            continue
        records.append(
            {
                "pid": pid,
                "start_ticks": start_ticks,
                "namespace": namespace,
                "namespace_inode": inode,
                "executable": str(executable),
                "executable_sha256": executable_hash,
                "cmdline": cmdline,
                "cap_eff": cap_eff,
                "cgroup": cgroup,
            }
        )
    return sorted(records, key=lambda item: item["pid"])


class TopologyMonitor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.commands = command_paths()
        self.base = args.run_dir / "raw/topology_monitor"
        self.control = self.base / "control"
        self.acks = self.base / "acks"
        self.netlink = self.base / "netlink"
        for path in (self.control, self.acks, self.netlink):
            path.mkdir(parents=True, exist_ok=True)
        self.samples_path = self.base / "samples.jsonl"
        self.samples = self.samples_path.open("x", encoding="utf-8")
        self.hasher = ExecutableHasher()
        self.sample_sequence = 0
        self.sample_times: list[int] = []
        self.transition_events: list[str] = []
        self.processed_commands: set[Path] = set()
        self.netlink_processes: dict[str, subprocess.Popen[bytes]] = {}
        self.netlink_handles: dict[str, tuple[Any, Any]] = {}

    def ensure_netlink_monitors(self) -> None:
        for namespace in NAMESPACE_NAMES:
            if (
                namespace in self.netlink_processes
                or namespace_inode(namespace) is None
            ):
                continue
            stdout_path = self.netlink / f"{namespace}.jsonl.txt"
            stderr_path = self.netlink / f"{namespace}.stderr"
            stdout = stdout_path.open("xb")
            stderr = stderr_path.open("xb")
            argv = [
                *namespace_prefix(namespace, self.commands),
                self.commands["ip"],
                "-ts",
                "monitor",
                "all",
            ]
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
            self.netlink_processes[namespace] = process
            self.netlink_handles[namespace] = (stdout, stderr)

    def sample(
        self,
        *,
        reason: str,
        transition_sequence: int | None = None,
        transition_event: str | None = None,
        command_sha256: str | None = None,
    ) -> None:
        self.ensure_netlink_monitors()
        with ThreadPoolExecutor(max_workers=len(NAMESPACE_NAMES)) as executor:
            futures = {
                name: executor.submit(collect_namespace, name, self.commands)
                for name in NAMESPACE_NAMES
            }
            namespaces = {name: futures[name].result() for name in NAMESPACE_NAMES}
        now = time.monotonic_ns()
        self.sample_sequence += 1
        record = {
            "schema": SAMPLE_SCHEMA,
            "run_id": self.args.run_id,
            "runtime_id": self.args.runtime_id,
            "run_nonce": self.args.run_nonce,
            "sample_sequence": self.sample_sequence,
            "monotonic_ns": now,
            "reason": reason,
            "transition_sequence": transition_sequence,
            "transition_event": transition_event,
            "command_sha256": command_sha256,
            "namespaces": namespaces,
            "processes": collect_processes(namespaces, self.hasher),
            "netlink_monitors": {
                name: {
                    "pid": process.pid,
                    "start_ticks": process_start_ticks(
                        Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8")
                    ),
                    "alive": process.poll() is None,
                }
                for name, process in sorted(self.netlink_processes.items())
            },
        }
        if any(
            value["alive"] is not True for value in record["netlink_monitors"].values()
        ):
            raise MonitorError("a namespace netlink monitor exited early")
        self.samples.write(canonical_json(record).decode())
        self.samples.flush()
        self.sample_times.append(now)
        if transition_event is not None:
            self.transition_events.append(transition_event)

    def process_control(self) -> bool:
        for path in sorted(self.control.glob("*.json")):
            if path in self.processed_commands:
                continue
            command_payload = path.read_bytes()
            command = strict_json(path)
            expected_keys = {
                "contract",
                "run_id",
                "runtime_id",
                "run_nonce",
                "action",
                "sequence",
                "event",
                "created_monotonic_ns",
            }
            if set(command) != expected_keys or (
                command.get("contract"),
                command.get("run_id"),
                command.get("runtime_id"),
                command.get("run_nonce"),
            ) != (
                COMMAND_CONTRACT,
                self.args.run_id,
                self.args.runtime_id,
                self.args.run_nonce,
            ):
                raise MonitorError(f"topology control identity/keys mismatch: {path}")
            action = command.get("action")
            sequence = command.get("sequence")
            event = command.get("event")
            if not isinstance(sequence, int) or sequence < 1:
                raise MonitorError(f"topology control sequence is invalid: {path}")
            if action == "transition":
                if not isinstance(event, str) or SAFE_EVENT.fullmatch(event) is None:
                    raise MonitorError(f"topology transition event is invalid: {path}")
                self.sample(
                    reason="transition",
                    transition_sequence=sequence,
                    transition_event=event,
                    command_sha256=hashlib.sha256(command_payload).hexdigest(),
                )
            elif action == "stop" and event == "monitor_stop":
                self.processed_commands.add(path)
                write_exclusive(
                    self.acks / f"{sequence:06d}-{event}.json",
                    {
                        "contract": ACK_CONTRACT,
                        "sequence": sequence,
                        "event": event,
                        "command_sha256": hashlib.sha256(command_payload).hexdigest(),
                        "sample_sequence": self.sample_sequence,
                        "ack_monotonic_ns": time.monotonic_ns(),
                    },
                )
                return True
            else:
                raise MonitorError(f"topology control action/event is invalid: {path}")
            self.processed_commands.add(path)
            write_exclusive(
                self.acks / f"{sequence:06d}-{event}.json",
                {
                    "contract": ACK_CONTRACT,
                    "sequence": sequence,
                    "event": event,
                    "command_sha256": hashlib.sha256(command_payload).hexdigest(),
                    "sample_sequence": self.sample_sequence,
                    "ack_monotonic_ns": time.monotonic_ns(),
                },
            )
        return False

    def close(self) -> None:
        netlink_summary: dict[str, Any] = {}
        for namespace, process in self.netlink_processes.items():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for namespace, process in self.netlink_processes.items():
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5)
            stdout, stderr = self.netlink_handles[namespace]
            stdout.close()
            stderr.close()
            stdout_path = self.netlink / f"{namespace}.jsonl.txt"
            stderr_path = self.netlink / f"{namespace}.stderr"
            netlink_summary[namespace] = {
                "pid": process.pid,
                "returncode": returncode,
                "stdout_path": stdout_path.relative_to(self.args.run_dir).as_posix(),
                "stdout_bytes": stdout_path.stat().st_size,
                "stderr_path": stderr_path.relative_to(self.args.run_dir).as_posix(),
                "stderr_bytes": stderr_path.stat().st_size,
            }
        self.samples.flush()
        os.fsync(self.samples.fileno())
        self.samples.close()
        max_gap = max(
            (
                right - left
                for left, right in zip(self.sample_times, self.sample_times[1:])
            ),
            default=0,
        )
        write_exclusive(
            self.base / "summary.json",
            {
                "contract": SUMMARY_CONTRACT,
                "run_id": self.args.run_id,
                "runtime_id": self.args.runtime_id,
                "run_nonce": self.args.run_nonce,
                "interval_ms": self.args.interval_ms,
                "sample_count": self.sample_sequence,
                "first_sample_monotonic_ns": self.sample_times[0]
                if self.sample_times
                else None,
                "last_sample_monotonic_ns": self.sample_times[-1]
                if self.sample_times
                else None,
                "maximum_sample_gap_ns": max_gap,
                "transition_events": self.transition_events,
                "command_paths": self.commands,
                "netlink_monitors": netlink_summary,
                "stopped_monotonic_ns": time.monotonic_ns(),
            },
        )

    def run(self) -> None:
        write_exclusive(
            self.base / "ready.json",
            {
                "contract": SUMMARY_CONTRACT,
                "run_id": self.args.run_id,
                "runtime_id": self.args.runtime_id,
                "run_nonce": self.args.run_nonce,
                "pid": os.getpid(),
                "start_ticks": process_start_ticks(
                    Path("/proc/self/stat").read_text(encoding="utf-8")
                ),
                "interval_ms": self.args.interval_ms,
                "command_paths": self.commands,
                "ready_monotonic_ns": time.monotonic_ns(),
            },
        )
        next_periodic_ns = time.monotonic_ns()
        stopped = False
        try:
            while not stopped:
                stopped = self.process_control()
                if stopped:
                    break
                now = time.monotonic_ns()
                if now >= next_periodic_ns:
                    self.sample(reason="periodic")
                    next_periodic_ns = now + self.args.interval_ms * 1_000_000
                time.sleep(0.025)
        finally:
            self.close()


def command_record(args: argparse.Namespace, action: str, event: str) -> dict[str, Any]:
    return {
        "contract": COMMAND_CONTRACT,
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "action": action,
        "sequence": args.sequence,
        "event": event,
        "created_monotonic_ns": time.monotonic_ns(),
    }


def submit_command(args: argparse.Namespace, action: str, event: str) -> None:
    if not SAFE_EVENT.fullmatch(event):
        raise MonitorError("event name is invalid")
    base = args.run_dir / "raw/topology_monitor"
    ready = base / "ready.json"
    if not ready.is_file():
        raise MonitorError("topology monitor is not ready")
    command_path = base / "control" / f"{args.sequence:06d}-{event}.json"
    ack_path = base / "acks" / f"{args.sequence:06d}-{event}.json"
    write_exclusive(command_path, command_record(args, action, event))
    deadline = time.monotonic() + args.timeout_s
    while time.monotonic() < deadline:
        if ack_path.is_file():
            return
        time.sleep(0.025)
    raise MonitorError(f"topology monitor did not acknowledge {event}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "notify", "stop"):
        child = subparsers.add_parser(name)
        child.add_argument("--run-dir", type=Path, required=True)
        child.add_argument("--run-id", required=True)
        child.add_argument("--runtime-id", required=True)
        child.add_argument("--run-nonce", required=True)
        if name == "run":
            child.add_argument("--interval-ms", type=int, default=500)
        else:
            child.add_argument("--sequence", type=int, required=True)
            child.add_argument("--timeout-s", type=float, default=5.0)
        if name == "notify":
            child.add_argument("--event", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            if not 100 <= args.interval_ms <= 500:
                raise MonitorError("interval_ms must be between 100 and 500")
            TopologyMonitor(args).run()
        elif args.command == "notify":
            submit_command(args, "transition", args.event)
        else:
            submit_command(args, "stop", "monitor_stop")
    except (MonitorError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"FAIL M3 topology monitor: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
