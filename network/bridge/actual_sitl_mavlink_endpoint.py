#!/usr/bin/env python3
"""Fail-closed, byte-opaque MAVLink bridge for one actual ArduPilot SITL UAV.

The bridge intentionally has no MAVLink encoder and never manufactures an ACK,
heartbeat, or telemetry frame.  It learns the dynamic MAVProxy UDP source only
from a real vehicle-system MAVLink datagram, publishes a candidate containing
both socket sides, and forwards bytes only after an independent supervisor has
authorized the exact process/socket lineage.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import stat
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.bridge.opaque_udp_relay import (  # noqa: E402
    ByteOpaqueUdpRelay,
    RelayError,
)
from network.bridge.runtime_clock_beacon import beacon  # noqa: E402

RELAY_CORE_SOURCE = ROOT_DIR / "network" / "bridge" / "opaque_udp_relay.py"


MANIFEST_CONTRACT = "ams.actual-sitl-endpoint-manifest/v1"
CANDIDATE_CONTRACT = "ams.actual-sitl-peer-candidate/v1"
AUTHORIZATION_CONTRACT = "ams.actual-sitl-endpoint-authorization/v1"
READY_CONTRACT = "ams.actual-sitl-endpoint-ready/v1"
FAILURE_CONTRACT = "ams.actual-sitl-endpoint-failure/v1"
EXPECTED_UAVS = tuple(f"uav{index}" for index in range(1, 6))
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 4 * 1024 * 1024
_EXECUTABLE_HASH_CACHE: dict[tuple[int, int, int, int, int], str] = {}


class EndpointError(RuntimeError):
    """The endpoint contract or live lineage is invalid."""


class LineageError(EndpointError):
    """A live PID/socket endpoint is absent, ambiguous, or replaced."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def document_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def executable_sha256(proc_exe: Path) -> str:
    info = proc_exe.stat()
    key = (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )
    digest = _EXECUTABLE_HASH_CACHE.get(key)
    if digest is None:
        digest = sha256_file(proc_exe)
        _EXECUTABLE_HASH_CACHE[key] = digest
    return digest


def _reject_symlink_parents(path: Path) -> None:
    probe = path.absolute()
    for parent in (probe.parent, *probe.parents):
        if parent.exists() and parent.is_symlink():
            raise EndpointError(f"symlinked path component is forbidden: {parent}")


def publish_json_exclusive(path: Path, value: Any, *, mode: int = 0o600) -> None:
    """Atomically publish immutable JSON; an existing destination is an error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_parents(path)
    payload = canonical_json(value)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise EndpointError(f"short write while publishing {path}")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as exc:
        raise EndpointError(f"immutable evidence already exists: {path}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def strict_json(path: Path) -> dict[str, Any]:
    """Read one non-symlink regular JSON object and reject duplicate keys."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise EndpointError(f"cannot stat {path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise EndpointError(f"JSON input must be a non-symlink regular file: {path}")
    if before.st_size <= 0 or before.st_size > MAX_JSON_BYTES:
        raise EndpointError(f"JSON input has invalid size: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            payload = b""
            while len(payload) <= MAX_JSON_BYTES:
                block = os.read(descriptor, min(65536, MAX_JSON_BYTES + 1 - len(payload)))
                if not block:
                    break
                payload += block
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise EndpointError(f"cannot read {path}: {exc}") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise EndpointError(f"JSON input is too large: {path}")
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise EndpointError(f"JSON input changed while being read: {path}")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EndpointError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EndpointError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EndpointError(f"JSON document is not an object: {path}")
    return value


class JsonlAudit:
    def __init__(self, path: Path, manifest: dict[str, Any], uav: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_parents(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._descriptor = os.open(path, flags, 0o600)
        self._identity = {
            "run_id": manifest["run_id"],
            "runtime_id": manifest["runtime_id"],
            "run_nonce": manifest["run_nonce"],
            "uav": uav,
        }
        self._sequence = 0
        self._last_fsync_ns = time.monotonic_ns()
        self._dirty = False
        self._previous_record_sha256: str | None = None

    def emit(self, event: str, **fields: Any) -> None:
        self._sequence += 1
        now_ns = time.monotonic_ns()
        record = {
            "schema_version": 1,
            **self._identity,
            "event_seq": self._sequence,
            "previous_record_sha256": self._previous_record_sha256,
            "event": event,
            "wall_utc": utc_now(),
            "monotonic_ns": now_ns,
            **fields,
        }
        payload = canonical_json(record)
        offset = 0
        while offset < len(payload):
            offset += os.write(self._descriptor, payload[offset:])
        self._previous_record_sha256 = hashlib.sha256(payload).hexdigest()
        self._dirty = True
        # Packet forwarding can generate hundreds of audit records per second.
        # Bound durability work to at most one fsync per second; an incomplete
        # crash artifact still fails strict sequence/hash-chain validation.
        if now_ns - self._last_fsync_ns >= 1_000_000_000:
            os.fsync(self._descriptor)
            self._last_fsync_ns = now_ns
            self._dirty = False

    def close(self) -> None:
        if self._dirty:
            os.fsync(self._descriptor)
            self._dirty = False
        os.close(self._descriptor)


def validate_jsonl_audit(
    path: Path,
    *,
    run_id: str,
    runtime_id: str,
    run_nonce: str,
    uav: str,
) -> list[dict[str, Any]]:
    """Strictly validate audit identity, sequence continuity, and hash chaining."""
    try:
        info = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise EndpointError(f"cannot read audit log {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not payload:
        raise EndpointError(f"audit log must be a nonempty non-symlink regular file: {path}")
    if not payload.endswith(b"\n"):
        raise EndpointError(f"audit log has an incomplete final record: {path}")
    records: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for sequence, line in enumerate(payload.splitlines(keepends=True), start=1):
        try:
            record = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise EndpointError(f"invalid audit JSON at line {sequence}: {exc}") from exc
        if not isinstance(record, dict):
            raise EndpointError(f"audit line {sequence} is not an object")
        if record.get("event_seq") != sequence:
            raise EndpointError(f"audit event sequence gap at line {sequence}")
        if record.get("previous_record_sha256") != previous_hash:
            raise EndpointError(f"audit hash-chain break at line {sequence}")
        if (
            record.get("run_id"),
            record.get("runtime_id"),
            record.get("run_nonce"),
            record.get("uav"),
        ) != (run_id, runtime_id, run_nonce, uav):
            raise EndpointError(f"audit identity mismatch at line {sequence}")
        previous_hash = hashlib.sha256(line).hexdigest()
        records.append(record)
    return records


def _exact_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise EndpointError(f"{context} keys differ; missing={missing}, extra={extra}")


def _valid_endpoint(value: Any, expected: tuple[str, int], context: str) -> None:
    if not isinstance(value, dict):
        raise EndpointError(f"{context} must be an endpoint object")
    _exact_keys(value, {"host", "port"}, context)
    if value["host"] != expected[0] or value["port"] != expected[1]:
        raise EndpointError(f"{context} must equal {expected[0]}:{expected[1]}")


PROCESS_IDENTITY_KEYS = {
    "pid",
    "ppid",
    "pgid",
    "session_id",
    "state",
    "start_ticks",
    "cmdline_b64",
    "cmdline_sha256",
    "argv",
    "exe_path",
    "exe_sha256",
    "exe_dev",
    "exe_inode",
    "exe_size",
    "netns_inode",
    "cgroup_sha256",
    "socket_inodes",
}

EXPECTED_PROCESS_KEYS = {
    "pid",
    "start_ticks",
    "pgid",
    "session_id",
    "cmdline_sha256",
    "exe_path",
    "exe_sha256",
    "exe_dev",
    "exe_inode",
    "exe_size",
    "netns_inode",
}


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        manifest,
        {
            "schema_version",
            "contract",
            "run_id",
            "runtime_id",
            "run_nonce",
            "adapter_source_sha256",
            "relay_core_source_sha256",
            "peer_lease_ms",
            "lineage_check_ms",
            "authorization_timeout_ms",
            "channels",
        },
        "manifest",
    )
    if manifest["schema_version"] != 1 or manifest["contract"] != MANIFEST_CONTRACT:
        raise EndpointError("unsupported actual-SITL endpoint manifest contract")
    for key in ("run_id", "runtime_id", "run_nonce"):
        if not isinstance(manifest[key], str) or SAFE_ID.fullmatch(manifest[key]) is None:
            raise EndpointError(f"manifest {key} is unsafe or empty")
    if not isinstance(manifest["adapter_source_sha256"], str) or HEX64.fullmatch(
        manifest["adapter_source_sha256"]
    ) is None:
        raise EndpointError("manifest adapter_source_sha256 is invalid")
    if not isinstance(manifest["relay_core_source_sha256"], str) or HEX64.fullmatch(
        manifest["relay_core_source_sha256"]
    ) is None:
        raise EndpointError("manifest relay_core_source_sha256 is invalid")
    for key, lower, upper in (
        ("peer_lease_ms", 1000, 60000),
        ("lineage_check_ms", 25, 5000),
        ("authorization_timeout_ms", 1000, 300000),
    ):
        value = manifest[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise EndpointError(f"manifest {key} must be in {lower}..{upper}")
    channels = manifest["channels"]
    if not isinstance(channels, list) or len(channels) != 5:
        raise EndpointError("manifest must contain exactly five endpoint channels")
    process_pids: set[int] = set()
    launch_pgids: set[int] = set()
    for index, channel in enumerate(channels, start=1):
        context = f"channels[{index - 1}]"
        if not isinstance(channel, dict):
            raise EndpointError(f"{context} must be an object")
        _exact_keys(
            channel,
            {
                "uav",
                "instance",
                "system_id",
                "namespace",
                "namespace_inode",
                "radio_bind",
                "gcs_peer",
                "tail_bind",
                "tail_peer_host",
                "tail_pcap_roles",
                "master",
                "launch_pgid",
                "mavproxy",
                "sitl",
            },
            context,
        )
        uav = f"uav{index}"
        expected_scalars = {
            "uav": uav,
            "instance": index - 1,
            "system_id": index,
            "namespace": f"ams-uav{index}",
            "tail_peer_host": f"10.72.{index}.1",
        }
        for key, expected in expected_scalars.items():
            if channel[key] != expected:
                raise EndpointError(f"{context}.{key} must equal {expected!r}")
        if (
            isinstance(channel["namespace_inode"], bool)
            or not isinstance(channel["namespace_inode"], int)
            or channel["namespace_inode"] <= 0
        ):
            raise EndpointError(f"{context}.namespace_inode must be positive")
        _valid_endpoint(
            channel["radio_bind"],
            (f"10.71.{index}.10", 14600 + index),
            f"{context}.radio_bind",
        )
        _valid_endpoint(channel["gcs_peer"], ("10.71.0.10", 14600), f"{context}.gcs_peer")
        _valid_endpoint(
            channel["tail_bind"],
            (f"10.72.{index}.2", 14559 + index),
            f"{context}.tail_bind",
        )
        _valid_endpoint(
            channel["master"],
            ("127.0.0.1", 5760 + 10 * (index - 1)),
            f"{context}.master",
        )
        pcap_roles = channel["tail_pcap_roles"]
        if pcap_roles != {
            "root": f"tail-root-uav{index}",
            "uav": f"tail-uav{index}",
        }:
            raise EndpointError(
                f"{context}.tail_pcap_roles must declare both actual tail capture roles"
            )
        launch_pgid = channel["launch_pgid"]
        if isinstance(launch_pgid, bool) or not isinstance(launch_pgid, int) or launch_pgid <= 1:
            raise EndpointError(f"{context}.launch_pgid must be a process group ID")
        launch_pgids.add(launch_pgid)
        for role in ("mavproxy", "sitl"):
            process = channel[role]
            if not isinstance(process, dict):
                raise EndpointError(f"{context}.{role} must be an object")
            _exact_keys(process, EXPECTED_PROCESS_KEYS, f"{context}.{role}")
            if any(
                isinstance(process[key], bool)
                or not isinstance(process[key], int)
                or process[key] <= 0
                for key in (
                    "pid",
                    "start_ticks",
                    "pgid",
                    "session_id",
                    "exe_dev",
                    "exe_inode",
                    "exe_size",
                    "netns_inode",
                )
            ):
                raise EndpointError(f"{context}.{role} contains invalid numeric identity fields")
            if process["pgid"] != launch_pgid:
                raise EndpointError(f"{context}.{role}.pgid differs from launch_pgid")
            for key in ("cmdline_sha256", "exe_sha256"):
                if not isinstance(process[key], str) or HEX64.fullmatch(process[key]) is None:
                    raise EndpointError(f"{context}.{role}.{key} is invalid")
            if not isinstance(process["exe_path"], str) or not process["exe_path"].startswith("/"):
                raise EndpointError(f"{context}.{role}.exe_path must be absolute")
            if process["pid"] in process_pids:
                raise EndpointError(f"process PID is reused across endpoint roles: {process['pid']}")
            process_pids.add(process["pid"])
    if len(launch_pgids) != 1:
        raise EndpointError("all five endpoint channels must share one exact launch_pgid")
    return manifest


def channel_by_uav(manifest: dict[str, Any], uav: str) -> dict[str, Any]:
    if uav not in EXPECTED_UAVS:
        raise EndpointError(f"unsupported UAV endpoint: {uav}")
    channel = next((item for item in manifest["channels"] if item["uav"] == uav), None)
    if channel is None:
        raise EndpointError(f"manifest has no channel for {uav}")
    return channel


def parse_proc_stat(payload: str) -> dict[str, Any]:
    opening = payload.find("(")
    closing = payload.rfind(")")
    if opening < 1 or closing <= opening:
        raise LineageError("malformed /proc stat record")
    fields = payload[closing + 1 :].strip().split()
    try:
        return {
            "pid": int(payload[:opening].strip()),
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "session_id": int(fields[3]),
            "start_ticks": int(fields[19]),
        }
    except (IndexError, ValueError) as exc:
        raise LineageError(f"malformed /proc stat fields: {exc}") from exc


def _netns_inode(path: Path) -> int:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise LineageError(f"cannot read network namespace identity {path}: {exc}") from exc
    match = re.fullmatch(r"net:\[([0-9]+)\]", target)
    if match is None:
        raise LineageError(f"unexpected network namespace identity: {target!r}")
    return int(match.group(1))


def socket_inodes(pid: int) -> list[int]:
    result: set[int] = set()
    try:
        descriptors = list((Path("/proc") / str(pid) / "fd").iterdir())
    except OSError as exc:
        raise LineageError(f"cannot enumerate PID {pid} descriptors: {exc}") from exc
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
        if match is not None:
            result.add(int(match.group(1)))
    return sorted(result)


def read_process_identity(pid: int, *, hash_executable: bool = True) -> dict[str, Any]:
    # PID 1 is a legitimate directly launched process inside a dedicated
    # acceptance container; start_ticks/cmdline/exe/netns still bind it exactly.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise LineageError(f"invalid process PID: {pid!r}")
    proc = Path("/proc") / str(pid)
    try:
        first = parse_proc_stat((proc / "stat").read_text(encoding="utf-8"))
        raw_cmdline = (proc / "cmdline").read_bytes()
        exe_path = os.readlink(proc / "exe")
        exe_stat = (proc / "exe").stat()
        netns_inode = _netns_inode(proc / "ns/net")
        cgroup = (proc / "cgroup").read_bytes()
        owned_sockets = socket_inodes(pid)
        second = parse_proc_stat((proc / "stat").read_text(encoding="utf-8"))
    except (OSError, LineageError) as exc:
        raise LineageError(f"cannot snapshot PID {pid}: {exc}") from exc
    if first["start_ticks"] != second["start_ticks"] or first["pid"] != second["pid"]:
        raise LineageError(f"PID {pid} was replaced while being inspected")
    if second["state"] in {"Z", "X", "x"}:
        raise LineageError(f"PID {pid} is not live (state={second['state']})")
    argv = [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw_cmdline.rstrip(b"\0").split(b"\0")
        if item
    ]
    if not argv:
        raise LineageError(f"PID {pid} has an empty command line")
    return {
        **second,
        "cmdline_b64": base64.b64encode(raw_cmdline).decode("ascii"),
        "cmdline_sha256": hashlib.sha256(raw_cmdline).hexdigest(),
        "argv": argv,
        "exe_path": exe_path,
        "exe_sha256": executable_sha256(proc / "exe") if hash_executable else None,
        "exe_dev": int(exe_stat.st_dev),
        "exe_inode": int(exe_stat.st_ino),
        "exe_size": int(exe_stat.st_size),
        "netns_inode": netns_inode,
        "cgroup_sha256": hashlib.sha256(cgroup).hexdigest(),
        "socket_inodes": owned_sockets,
    }


def expected_process_identity(identity: dict[str, Any]) -> dict[str, Any]:
    if set(identity) != PROCESS_IDENTITY_KEYS:
        raise EndpointError("cannot derive expected identity from incomplete process snapshot")
    return {key: identity[key] for key in EXPECTED_PROCESS_KEYS}


def verify_expected_process(
    role: str,
    expected: dict[str, Any],
    *,
    hash_executable: bool = True,
) -> dict[str, Any]:
    actual = read_process_identity(expected["pid"], hash_executable=hash_executable)
    compare = EXPECTED_PROCESS_KEYS if hash_executable else EXPECTED_PROCESS_KEYS - {"exe_sha256"}
    differences = {
        key: {"expected": expected[key], "actual": actual[key]}
        for key in sorted(compare)
        if actual[key] != expected[key]
    }
    if differences:
        raise LineageError(f"{role} process identity changed: {differences}")
    return actual


def _decode_ipv4(value: str) -> str:
    try:
        return socket.inet_ntoa(struct.pack("<I", int(value, 16)))
    except (ValueError, OSError, struct.error) as exc:
        raise LineageError(f"invalid /proc IPv4 field {value!r}") from exc


def _decode_address(value: str) -> dict[str, Any]:
    host_hex, separator, port_hex = value.partition(":")
    if not separator:
        raise LineageError(f"invalid /proc socket address: {value!r}")
    return {"host": _decode_ipv4(host_hex), "port": int(port_hex, 16)}


def parse_proc_inet(payload: str, protocol: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in payload.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        try:
            records.append(
                {
                    "protocol": protocol,
                    "local": _decode_address(fields[1]),
                    "remote": _decode_address(fields[2]),
                    "state": fields[3],
                    "uid": int(fields[7]),
                    "inode": int(fields[9]),
                }
            )
        except (ValueError, LineageError) as exc:
            raise LineageError(f"cannot parse /proc/net/{protocol} row: {line!r}: {exc}") from exc
    return records


def owned_inet_records(pid: int, protocol: str, owned: Iterable[int] | None = None) -> list[dict[str, Any]]:
    if protocol not in {"udp", "tcp"}:
        raise ValueError(f"unsupported protocol: {protocol}")
    inodes = set(socket_inodes(pid) if owned is None else owned)
    try:
        payload = (Path("/proc") / str(pid) / "net" / protocol).read_text(encoding="ascii")
    except OSError as exc:
        raise LineageError(f"cannot read PID {pid} {protocol} socket table: {exc}") from exc
    return [record for record in parse_proc_inet(payload, protocol) if record["inode"] in inodes]


def _flag_values(argv: list[str], flag: str) -> list[str]:
    result: list[str] = []
    prefix = flag + "="
    for index, value in enumerate(argv):
        if value == flag and index + 1 < len(argv):
            result.append(argv[index + 1])
        elif value.startswith(prefix):
            result.append(value[len(prefix) :])
    return result


def _assert_process_roles(channel: dict[str, Any], mavproxy: dict[str, Any], sitl: dict[str, Any]) -> None:
    mav_names = [Path(value).name.lower() for value in mavproxy["argv"][:3]]
    if "mavproxy.py" not in mav_names:
        raise LineageError(f"{channel['uav']} MAVProxy PID argv does not execute mavproxy.py")
    tail = channel["tail_bind"]
    expected_out = f"{tail['host']}:{tail['port']}"
    if _flag_values(mavproxy["argv"], "--out") != [expected_out]:
        raise LineageError(
            f"{channel['uav']} MAVProxy must have exactly one --out {expected_out}"
        )
    master = channel["master"]
    expected_master = f"tcp:{master['host']}:{master['port']}"
    if _flag_values(mavproxy["argv"], "--master") != [expected_master]:
        raise LineageError(
            f"{channel['uav']} MAVProxy must have exactly one --master {expected_master}"
        )
    if Path(sitl["exe_path"]).name.lower() != "arducopter":
        raise LineageError(f"{channel['uav']} SITL executable is not arducopter")
    if Path(sitl["argv"][0]).name.lower() != "arducopter":
        raise LineageError(f"{channel['uav']} SITL argv[0] is not arducopter")


def _one(records: list[dict[str, Any]], context: str) -> dict[str, Any]:
    if len(records) != 1:
        raise LineageError(f"{context} must resolve to exactly one socket, found {len(records)}")
    return records[0]


def verify_adapter_sockets(
    adapter_pid: int,
    channel: dict[str, Any],
    radio_inode: int,
    tail_inode: int,
) -> dict[str, Any]:
    identity = read_process_identity(adapter_pid)
    if identity["netns_inode"] != channel["namespace_inode"]:
        raise LineageError(f"{channel['uav']} adapter is outside its declared network namespace")
    if radio_inode == tail_inode or radio_inode not in identity["socket_inodes"] or tail_inode not in identity["socket_inodes"]:
        raise LineageError(f"{channel['uav']} adapter socket inode ownership is invalid")
    records = owned_inet_records(adapter_pid, "udp", identity["socket_inodes"])

    def matches(record: dict[str, Any], endpoint: dict[str, Any], inode: int) -> bool:
        return record["inode"] == inode and record["local"] == endpoint and record["state"] == "07"

    radio = _one(
        [record for record in records if matches(record, channel["radio_bind"], radio_inode)],
        f"{channel['uav']} adapter radio bind",
    )
    tail = _one(
        [record for record in records if matches(record, channel["tail_bind"], tail_inode)],
        f"{channel['uav']} adapter tail bind",
    )
    return {"identity": identity, "radio_socket": radio, "tail_socket": tail}


def verify_channel_lineage(
    channel: dict[str, Any],
    mavproxy_peer: tuple[str, int],
    *,
    hash_executable: bool = True,
) -> dict[str, Any]:
    if mavproxy_peer[0] != channel["tail_peer_host"] or not 1 <= mavproxy_peer[1] <= 65535:
        raise LineageError(f"{channel['uav']} dynamic MAVProxy peer is outside the tail contract")
    mavproxy = verify_expected_process(
        f"{channel['uav']} mavproxy", channel["mavproxy"], hash_executable=hash_executable
    )
    sitl = verify_expected_process(
        f"{channel['uav']} sitl", channel["sitl"], hash_executable=hash_executable
    )
    if mavproxy["pgid"] != channel["launch_pgid"] or sitl["pgid"] != channel["launch_pgid"]:
        raise LineageError(f"{channel['uav']} process escaped the exact launch process group")
    _assert_process_roles(channel, mavproxy, sitl)

    mav_udp = owned_inet_records(mavproxy["pid"], "udp", mavproxy["socket_inodes"])
    tail = channel["tail_bind"]
    udp_owner = _one(
        [
            record
            for record in mav_udp
            if record["local"]["port"] == mavproxy_peer[1]
            and record["local"]["host"] in {"0.0.0.0", mavproxy_peer[0]}
            and record["remote"]["host"] in {"0.0.0.0", tail["host"]}
            and record["remote"]["port"] in {0, tail["port"]}
            and record["state"] == "07"
        ],
        f"{channel['uav']} dynamic MAVProxy UDP peer owner",
    )

    mav_tcp = owned_inet_records(mavproxy["pid"], "tcp", mavproxy["socket_inodes"])
    master = channel["master"]
    mav_master = _one(
        [
            record
            for record in mav_tcp
            if record["state"] == "01"
            and record["remote"] == master
            and record["local"]["host"] == "127.0.0.1"
        ],
        f"{channel['uav']} MAVProxy-to-SITL established TCP master",
    )
    sitl_tcp = owned_inet_records(sitl["pid"], "tcp", sitl["socket_inodes"])
    sitl_master = _one(
        [
            record
            for record in sitl_tcp
            if record["state"] == "01"
            and record["local"] == master
            and record["remote"] == mav_master["local"]
        ],
        f"{channel['uav']} SITL accepted TCP master",
    )
    _one(
        [
            record
            for record in sitl_tcp
            if record["state"] == "0A"
            and record["local"]["port"] == master["port"]
            and record["local"]["host"] in {"0.0.0.0", master["host"]}
        ],
        f"{channel['uav']} SITL TCP master listener",
    )
    return {
        "mavproxy": mavproxy,
        "sitl": sitl,
        "mavproxy_udp_peer": {"host": mavproxy_peer[0], "port": mavproxy_peer[1]},
        "mavproxy_udp_socket": udp_owner,
        "mavproxy_tcp_master": mav_master,
        "sitl_tcp_master": sitl_master,
    }


def socket_inode(sock: socket.socket) -> int:
    target = os.readlink(Path("/proc/self/fd") / str(sock.fileno()))
    match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
    if match is None:
        raise LineageError(f"file descriptor {sock.fileno()} is not a socket")
    return int(match.group(1))


def mavlink_source_system_ids(payload: bytes) -> list[int]:
    """Extract sysids from complete MAVLink v1/v2 frames without decoding payloads."""
    result: list[int] = []
    offset = 0
    while offset < len(payload):
        magic = payload[offset]
        if magic == 0xFE:
            if offset + 8 > len(payload):
                break
            frame_size = int(payload[offset + 1]) + 8
            if offset + frame_size > len(payload):
                break
            result.append(int(payload[offset + 3]))
            offset += frame_size
        elif magic == 0xFD:
            if offset + 12 > len(payload):
                break
            signature_size = 13 if payload[offset + 2] & 0x01 else 0
            frame_size = int(payload[offset + 1]) + 12 + signature_size
            if offset + frame_size > len(payload):
                break
            result.append(int(payload[offset + 5]))
            offset += frame_size
        else:
            offset += 1
    return result


def validate_authorization(
    authorization: dict[str, Any],
    manifest: dict[str, Any],
    manifest_hash: str,
    channel: dict[str, Any],
    candidate: dict[str, Any],
    candidate_hash: str,
) -> None:
    _exact_keys(
        authorization,
        {
            "schema_version",
            "contract",
            "status",
            "run_id",
            "runtime_id",
            "run_nonce",
            "uav",
            "manifest_sha256",
            "candidate_sha256",
            "verified_candidate_lineage_sha256",
            "issuer",
            "authorized_wall_utc",
            "authorized_monotonic_ns",
        },
        "authorization",
    )
    expected = {
        "schema_version": 1,
        "contract": AUTHORIZATION_CONTRACT,
        "status": "authorized",
        "run_id": manifest["run_id"],
        "runtime_id": manifest["runtime_id"],
        "run_nonce": manifest["run_nonce"],
        "uav": channel["uav"],
        "manifest_sha256": manifest_hash,
        "candidate_sha256": candidate_hash,
        "verified_candidate_lineage_sha256": document_sha256(candidate["lineage"]),
    }
    differences = {
        key: {"expected": value, "actual": authorization.get(key)}
        for key, value in expected.items()
        if authorization.get(key) != value
    }
    if differences:
        raise EndpointError(f"authorization does not match the immutable candidate: {differences}")
    issuer = authorization["issuer"]
    if not isinstance(issuer, dict) or set(issuer) != PROCESS_IDENTITY_KEYS:
        raise EndpointError("authorization issuer identity is incomplete")
    verify_expected_process("authorization issuer", expected_process_identity(issuer))


def _endpoint_tuple(value: dict[str, Any]) -> tuple[str, int]:
    return str(value["host"]), int(value["port"])


def _adapter_paths(run_dir: Path, uav: str) -> dict[str, Path]:
    evidence = run_dir / "raw" / "actual_sitl"
    return {
        "candidate": evidence / f"{uav}.peer-candidate.json",
        "authorization": evidence / f"{uav}.authorization.json",
        "ready": evidence / f"{uav}.ready.json",
        "failure": evidence / f"{uav}.failure.json",
        "log": run_dir / "logs" / f"actual_sitl_{uav}.jsonl",
    }


def _validate_adapter_candidate_base(
    candidate: dict[str, Any], manifest: dict[str, Any], manifest_hash: str, channel: dict[str, Any]
) -> None:
    _exact_keys(
        candidate,
        {
            "schema_version",
            "contract",
            "status",
            "run_id",
            "runtime_id",
            "run_nonce",
            "uav",
            "system_id",
            "manifest_sha256",
            "created_wall_utc",
            "created_monotonic_ns",
            "adapter",
            "radio_socket",
            "tail_socket",
            "mavproxy_peer",
            "first_tail_datagram",
            "lineage",
        },
        "candidate",
    )
    expected = {
        "schema_version": 1,
        "contract": CANDIDATE_CONTRACT,
        "status": "awaiting_external_authorization",
        "run_id": manifest["run_id"],
        "runtime_id": manifest["runtime_id"],
        "run_nonce": manifest["run_nonce"],
        "uav": channel["uav"],
        "system_id": channel["system_id"],
        "manifest_sha256": manifest_hash,
    }
    if any(candidate.get(key) != value for key, value in expected.items()):
        raise EndpointError(f"candidate identity differs from manifest for {channel['uav']}")


def run_endpoint(args: argparse.Namespace) -> int:
    if args.run_dir.is_symlink():
        raise EndpointError("run directory symlinks are forbidden")
    run_dir = args.run_dir.resolve(strict=True)
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise EndpointError("run directory must be a canonical non-symlink directory")
    if args.manifest.is_symlink():
        raise EndpointError("manifest symlinks are forbidden")
    manifest_path = args.manifest.resolve(strict=True)
    try:
        manifest_path.relative_to(run_dir)
    except ValueError as exc:
        raise EndpointError("manifest must be inside the run directory") from exc
    manifest = validate_manifest(strict_json(manifest_path))
    source_hash = sha256_file(Path(__file__).resolve())
    if source_hash != manifest["adapter_source_sha256"]:
        raise EndpointError("running adapter source differs from the manifested source hash")
    if sha256_file(RELAY_CORE_SOURCE) != manifest["relay_core_source_sha256"]:
        raise EndpointError("shared byte-opaque relay core differs from its manifested hash")
    channel = channel_by_uav(manifest, args.uav)
    manifest_hash = document_sha256(manifest)
    paths = _adapter_paths(run_dir, args.uav)
    audit = JsonlAudit(paths["log"], manifest, args.uav)
    stop = False
    clock_stop = threading.Event()
    clock_thread: threading.Thread | None = None

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    selector = selectors.DefaultSelector()
    radio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tail = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (radio, tail):
        sock.setblocking(False)
    counters = {
        "tail_to_gcs": 0,
        "gcs_to_tail": 0,
        "preauthorization_tail_held": 0,
        "preauthorization_radio_dropped": 0,
        "unexpected_radio_dropped": 0,
        "unqualified_tail_dropped": 0,
    }
    candidate: dict[str, Any] | None = None
    candidate_hash: str | None = None
    buffered_tail: tuple[bytes, tuple[str, int]] | None = None
    last_tail_ns: int | None = None
    last_full_check_ns = 0
    authorized = False

    try:
        if getattr(args, "clock_socket", None) is not None:
            clock_thread = threading.Thread(
                target=beacon,
                args=(
                    args.clock_socket.resolve(),
                    f"uav_control_adapter_{args.uav}",
                    clock_stop,
                ),
                name=f"{args.uav}-actual-endpoint-clock",
            )
            clock_thread.start()
        radio.bind(_endpoint_tuple(channel["radio_bind"]))
        tail.bind(_endpoint_tuple(channel["tail_bind"]))
        selector.register(radio, selectors.EVENT_READ, "radio")
        selector.register(tail, selectors.EVENT_READ, "tail")
        radio_inode = socket_inode(radio)
        tail_inode = socket_inode(tail)
        adapter_sockets = verify_adapter_sockets(os.getpid(), channel, radio_inode, tail_inode)
        adapter_identity = adapter_sockets["identity"]
        adapter_identity.pop("exe_sha256", None)
        adapter_identity["exe_sha256"] = executable_sha256(Path("/proc/self/exe"))
        forwarder = ByteOpaqueUdpRelay(
            radio,
            tail,
            _endpoint_tuple(channel["gcs_peer"]),
            tail_peer_host=channel["tail_peer_host"],
            strict_tail_peer=True,
            forwarding_enabled=False,
            before_forward=lambda: None,
        )

        def current_lineage(*, full: bool) -> dict[str, Any]:
            nonlocal last_full_check_ns
            if forwarder.mavproxy_peer is None:
                raise LineageError("MAVProxy peer is not learned")
            current_adapter = verify_adapter_sockets(os.getpid(), channel, radio_inode, tail_inode)
            expected_adapter = expected_process_identity(adapter_identity)
            actual_adapter = current_adapter["identity"]
            compare = EXPECTED_PROCESS_KEYS if full else EXPECTED_PROCESS_KEYS - {"exe_sha256"}
            if any(actual_adapter[key] != expected_adapter[key] for key in compare):
                raise LineageError("adapter process identity changed after bind")
            lineage = verify_channel_lineage(
                channel, forwarder.mavproxy_peer, hash_executable=full
            )
            if full:
                last_full_check_ns = time.monotonic_ns()
            return lineage

        forwarder.lineage_check = lambda: current_lineage(full=False)
        audit.emit(
            "adapter_bound_not_ready",
            pid=os.getpid(),
            namespace_inode=adapter_identity["netns_inode"],
            radio_bind=channel["radio_bind"],
            radio_socket_inode=radio_inode,
            tail_bind=channel["tail_bind"],
            tail_socket_inode=tail_inode,
            gcs_peer=channel["gcs_peer"],
        )
        deadline_ns = time.monotonic_ns() + manifest["authorization_timeout_ms"] * 1_000_000

        while not stop:
            now_ns = time.monotonic_ns()
            if now_ns >= deadline_ns and not authorized:
                raise LineageError("external endpoint authorization timed out")
            if candidate is not None and last_tail_ns is not None:
                if now_ns - last_tail_ns > manifest["peer_lease_ms"] * 1_000_000:
                    raise LineageError("dynamic MAVProxy tail peer lease expired")
                if now_ns - last_full_check_ns >= manifest["lineage_check_ms"] * 1_000_000:
                    current_lineage(full=True)

            if candidate is not None and not authorized and paths["authorization"].exists():
                authorization = strict_json(paths["authorization"])
                assert candidate_hash is not None
                validate_authorization(
                    authorization,
                    manifest,
                    manifest_hash,
                    channel,
                    candidate,
                    candidate_hash,
                )
                lineage = current_lineage(full=True)
                forwarder.authorize()
                authorization_hash = document_sha256(authorization)
                ready = {
                    "schema_version": 1,
                    "contract": READY_CONTRACT,
                    "status": "ready",
                    "run_id": manifest["run_id"],
                    "runtime_id": manifest["runtime_id"],
                    "run_nonce": manifest["run_nonce"],
                    "uav": channel["uav"],
                    "system_id": channel["system_id"],
                    "manifest_sha256": manifest_hash,
                    "candidate_sha256": candidate_hash,
                    "authorization_sha256": authorization_hash,
                    "ready_wall_utc": utc_now(),
                    "ready_monotonic_ns": time.monotonic_ns(),
                    "adapter": adapter_identity,
                    "radio_socket": adapter_sockets["radio_socket"],
                    "tail_socket": adapter_sockets["tail_socket"],
                    "mavproxy_peer": {
                        "host": forwarder.mavproxy_peer[0],
                        "port": forwarder.mavproxy_peer[1],
                    },
                    "lineage": lineage,
                }
                publish_json_exclusive(paths["ready"], ready)
                authorized = True
                audit.emit(
                    "adapter_ready",
                    candidate_sha256=candidate_hash,
                    authorization_sha256=authorization_hash,
                    ready_sha256=document_sha256(ready),
                )
                if buffered_tail is not None:
                    payload, peer = buffered_tail
                    if forwarder.relay_tail(payload, peer).action == "forwarded":
                        counters["tail_to_gcs"] += 1
                        audit.emit(
                            "forward",
                            direction="tail_to_gcs",
                            source={"host": peer[0], "port": peer[1]},
                            destination=channel["gcs_peer"],
                            bytes=len(payload),
                            sha256=hashlib.sha256(payload).hexdigest(),
                            buffered_pre_authorization=True,
                        )
                    buffered_tail = None

            timeout = min(0.1, manifest["lineage_check_ms"] / 1000.0)
            for key, _mask in selector.select(timeout=timeout):
                sock = key.fileobj
                try:
                    payload, peer = sock.recvfrom(65535)
                except BlockingIOError:
                    continue
                peer = (str(peer[0]), int(peer[1]))
                digest = hashlib.sha256(payload).hexdigest()
                if key.data == "tail":
                    if peer[0] != channel["tail_peer_host"]:
                        counters["unqualified_tail_dropped"] += 1
                        audit.emit(
                            "drop",
                            direction="tail_to_gcs",
                            reason="unexpected_tail_host",
                            source={"host": peer[0], "port": peer[1]},
                            bytes=len(payload),
                            sha256=digest,
                        )
                        continue
                    if forwarder.mavproxy_peer is not None:
                        forwarder.lock_peer(peer)
                    if candidate is None:
                        source_systems = mavlink_source_system_ids(payload)
                        if channel["system_id"] not in source_systems:
                            counters["unqualified_tail_dropped"] += 1
                            audit.emit(
                                "drop",
                                direction="tail_to_gcs",
                                reason="tail_datagram_has_no_expected_vehicle_sysid",
                                source={"host": peer[0], "port": peer[1]},
                                mavlink_source_system_ids=source_systems,
                                bytes=len(payload),
                                sha256=digest,
                            )
                            continue
                        forwarder.lock_peer(peer)
                        lineage = verify_channel_lineage(channel, peer, hash_executable=True)
                        adapter_snapshot = verify_adapter_sockets(
                            os.getpid(), channel, radio_inode, tail_inode
                        )
                        candidate = {
                            "schema_version": 1,
                            "contract": CANDIDATE_CONTRACT,
                            "status": "awaiting_external_authorization",
                            "run_id": manifest["run_id"],
                            "runtime_id": manifest["runtime_id"],
                            "run_nonce": manifest["run_nonce"],
                            "uav": channel["uav"],
                            "system_id": channel["system_id"],
                            "manifest_sha256": manifest_hash,
                            "created_wall_utc": utc_now(),
                            "created_monotonic_ns": time.monotonic_ns(),
                            "adapter": adapter_snapshot["identity"],
                            "radio_socket": adapter_snapshot["radio_socket"],
                            "tail_socket": adapter_snapshot["tail_socket"],
                            "mavproxy_peer": {"host": peer[0], "port": peer[1]},
                            "first_tail_datagram": {
                                "bytes": len(payload),
                                "sha256": digest,
                                "mavlink_source_system_ids": source_systems,
                            },
                            "lineage": lineage,
                        }
                        _validate_adapter_candidate_base(
                            candidate, manifest, manifest_hash, channel
                        )
                        candidate_hash = document_sha256(candidate)
                        publish_json_exclusive(paths["candidate"], candidate)
                        buffered_tail = (payload, peer)
                        last_tail_ns = time.monotonic_ns()
                        counters["preauthorization_tail_held"] += 1
                        audit.emit(
                            "peer_candidate_published_not_ready",
                            source={"host": peer[0], "port": peer[1]},
                            candidate_sha256=candidate_hash,
                            lineage_sha256=document_sha256(lineage),
                            bytes=len(payload),
                            sha256=digest,
                        )
                    elif not authorized:
                        last_tail_ns = time.monotonic_ns()
                        counters["preauthorization_tail_held"] += 1
                        audit.emit(
                            "drop",
                            direction="tail_to_gcs",
                            reason="awaiting_external_authorization",
                            source={"host": peer[0], "port": peer[1]},
                            bytes=len(payload),
                            sha256=digest,
                        )
                    else:
                        last_tail_ns = time.monotonic_ns()
                        if forwarder.relay_tail(payload, peer).action == "forwarded":
                            counters["tail_to_gcs"] += 1
                            audit.emit(
                                "forward",
                                direction="tail_to_gcs",
                                source={"host": peer[0], "port": peer[1]},
                                destination=channel["gcs_peer"],
                                bytes=len(payload),
                                sha256=digest,
                                buffered_pre_authorization=False,
                            )
                elif peer != _endpoint_tuple(channel["gcs_peer"]):
                    counters["unexpected_radio_dropped"] += 1
                    audit.emit(
                        "drop",
                        direction="gcs_to_tail",
                        reason="unexpected_gcs_peer",
                        source={"host": peer[0], "port": peer[1]},
                        bytes=len(payload),
                        sha256=digest,
                    )
                elif not authorized:
                    counters["preauthorization_radio_dropped"] += 1
                    audit.emit(
                        "drop",
                        direction="gcs_to_tail",
                        reason="endpoint_not_externally_authorized",
                        source=channel["gcs_peer"],
                        bytes=len(payload),
                        sha256=digest,
                    )
                elif forwarder.relay_radio(payload, peer).action == "forwarded":
                    counters["gcs_to_tail"] += 1
                    assert forwarder.mavproxy_peer is not None
                    audit.emit(
                        "forward",
                        direction="gcs_to_tail",
                        source=channel["gcs_peer"],
                        destination={
                            "host": forwarder.mavproxy_peer[0],
                            "port": forwarder.mavproxy_peer[1],
                        },
                        bytes=len(payload),
                        sha256=digest,
                    )
        audit.emit("adapter_stop", reason="signal", authorized=authorized, counters=counters)
        return 0
    except (EndpointError, RelayError, OSError) as exc:
        failure = {
            "schema_version": 1,
            "contract": FAILURE_CONTRACT,
            "status": "failed_closed",
            "run_id": manifest["run_id"],
            "runtime_id": manifest["runtime_id"],
            "run_nonce": manifest["run_nonce"],
            "uav": channel["uav"],
            "failure_wall_utc": utc_now(),
            "failure_monotonic_ns": time.monotonic_ns(),
            "reason": str(exc),
            "authorized_before_failure": authorized,
            "counters": counters,
        }
        try:
            publish_json_exclusive(paths["failure"], failure)
        except EndpointError:
            pass
        audit.emit("adapter_failed_closed", reason=str(exc), authorized=authorized, counters=counters)
        print(f"FAIL {channel['uav']} actual-SITL endpoint: {exc}", file=sys.stderr)
        return 2
    finally:
        clock_stop.set()
        if clock_thread is not None:
            clock_thread.join(timeout=2.0)
            if clock_thread.is_alive():
                print(
                    f"FAIL {args.uav} actual-SITL clock beacon did not stop",
                    file=sys.stderr,
                )
        selector.close()
        radio.close()
        tail.close()
        audit.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--uav", choices=EXPECTED_UAVS, required=True)
    parser.add_argument("--clock-socket", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_endpoint(args)
    except EndpointError as exc:
        print(f"FAIL actual-SITL endpoint startup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
