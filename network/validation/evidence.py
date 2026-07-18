#!/usr/bin/env python3
"""Independent evidence inspection for the network/radio v3 contract.

Runtime producers and post-processors are intentionally not trusted to declare
P0 success. This module derives gate status from raw packet captures, counters,
structured experiment records, and process evidence.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import stat
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable

import yaml


TRAFFIC_CLASSES = ("control", "payload", "additional_data")
PACKET_KEYS = {
    "control": ("control_tx", "control_rx"),
    "payload": ("payload_tx", "payload_rx"),
    "additional_data": ("additional_tx", "additional_rx"),
}
REQUIRED_LATENCY_KEYS = ("control_p50", "control_p95", "payload_p50", "payload_p95")
P0_GATE_IDS = (
    "dependency_check",
    "provenance",
    "joint_runtime",
    "five_uav_health",
    "packet_provenance",
    "no_bypass",
    "three_traffic_classes",
    "online_sionna",
    "sionna_causality",
    "link_locality",
    "shared_medium",
    "priority",
    "jamming",
    "time_coherence",
    "scene_alignment",
    "heatmaps",
    "artifacts",
    "repeatability",
)
CRITICAL_LOG_PATTERNS = (
    "operation not permitted",
    "permission denied",
    "bind error",
    "segmentation fault",
    "core dumped",
    "traceback (most recent call last)",
)
ROOT_DIR = Path(__file__).resolve().parents[2]
JOINT_RUNTIME_EVENT_LOG = "logs/joint_runtime_events.jsonl"
FIVE_UAV_HEALTH_EVENT_LOG = "logs/five_uav_health_events.jsonl"
FIVE_UAV_LAUNCH_LOG = "logs/five_uav_launch.log"
NO_BYPASS_EVENT_LOG = "logs/no_bypass_events.jsonl"
MAVLINK_TRANSACTION_LOG = "logs/mavlink_transactions.jsonl"
M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
M1_DEPENDENCY_LOCK_PATH = "network/config/dependency_lock.yaml"
M1_PROFILE = "m1_component"
M1_SCENARIO_ID = "scenario_5uav"
M1_PHASES = ("readiness", "measurement", "finalization")
INHERITED_M0_RECEIPT_MOUNTS = {
    "m1_component": Path("/run/ams/m0-receipt.json"),
    "flight_capacity_prerequisite": Path("/run/ams/prerequisites/m0.json"),
}
M1_REQUIRED_PROCESS_COUNTS = {
    "arducopter": 5,
    "mavproxy": 5,
    "micro_ros_agent": 5,
    "gazebo_server": 1,
}
M1_MAVPROXY_OFFLINE_DEFAULT_MODULES = (
    "log,signing,wp,rally,fence,ftp,param,relay,tuneopt,arm,mode,calibration,"
    "rc,auxopt,misc,cmdlong,battery,output,adsb,layout"
)
M1_PROCESS_NAMESPACES = ("cgroup", "ipc", "mnt", "net", "pid", "user", "uts")
M1_CAPABILITIES = ("inheritable", "permitted", "effective", "bounding", "ambient")
M1_ALLOWED_PROCESS_STATES = {"R", "S", "D", "I"}
M1_PROCESS_SAMPLE_MAX_GAP_S = 1.5


class _BoundedFailureList(list[str]):
    """Keep fail-closed diagnostics useful without emitting unbounded JSON."""

    def __init__(self, limit: int = 2000) -> None:
        super().__init__()
        self.limit = limit
        self.total = 0

    def append(self, value: str) -> None:
        self.total += 1
        if len(self) < self.limit:
            super().append(value)

    def extend(self, values: Iterable[str]) -> None:
        for value in values:
            self.append(value)

    def __iadd__(self, values: Iterable[str]) -> "_BoundedFailureList":
        self.extend(values)
        return self


def gate(status: str, proof: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "proof": proof}
    if details is not None:
        result["details"] = details
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def read_exit_code(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _runtime_security_observation() -> dict[str, object]:
    """Independently observe the validator process capability boundary."""

    required = ("CapPrm", "CapEff", "CapBnd", "NoNewPrivs")
    parsed: dict[str, str] = {}
    try:
        lines = Path("/proc/self/status").read_text(
            encoding="ascii", errors="strict"
        ).splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in required and key not in parsed:
            parsed[key] = value.strip()

    def succeeds(command: list[str]) -> bool:
        try:
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    no_new_privs = parsed.get("NoNewPrivs")
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "CapPrm": parsed.get("CapPrm"),
        "CapEff": parsed.get("CapEff"),
        "CapBnd": parsed.get("CapBnd"),
        "NoNewPrivs": (
            int(no_new_privs)
            if isinstance(no_new_privs, str) and no_new_privs in {"0", "1"}
            else None
        ),
        "dev_net_tun": Path("/dev/net/tun").is_char_device(),
        "unshare_network_namespace": succeeds(["/usr/bin/unshare", "-rn", "true"]),
        "passwordless_sudo": succeeds(["/usr/bin/sudo", "-n", "true"]),
    }


def _bounded_root_runtime_evidence(
    consumption: Any, observed_network: Any
) -> tuple[bool, bool, bool, dict[str, object]]:
    """Validate producer facts and independently re-observe the same boundary."""

    from network.validation.qualification_identity import (  # noqa: PLC0415
        BOUNDED_ROOT_CAPABILITY_MASK,
        BOUNDED_ROOT_GID,
        BOUNDED_ROOT_NO_NEW_PRIVS,
        BOUNDED_ROOT_UID,
        is_exact_bounded_root_capability_mode,
    )

    network = observed_network if isinstance(observed_network, dict) else {}
    exact_mode = is_exact_bounded_root_capability_mode(
        consumption, network.get("qualification_mode")
    )
    expected = {
        "uid": BOUNDED_ROOT_UID,
        "gid": BOUNDED_ROOT_GID,
        "CapPrm": BOUNDED_ROOT_CAPABILITY_MASK,
        "CapEff": BOUNDED_ROOT_CAPABILITY_MASK,
        "CapBnd": BOUNDED_ROOT_CAPABILITY_MASK,
        "NoNewPrivs": BOUNDED_ROOT_NO_NEW_PRIVS,
        "dev_net_tun": True,
        "unshare_network_namespace": True,
        "passwordless_sudo": False,
    }
    recorded_exact = exact_mode and all(
        network.get(key) == value for key, value in expected.items()
    )
    independently_observed_exact = (
        exact_mode and _runtime_security_observation() == expected
    )
    return exact_mode, recorded_exact, independently_observed_exact, expected


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def component_python_runtime_failures(
    qualification_profile: Any, dependency_versions: Any
) -> list[str]:
    """Validate and independently re-observe a component Python runtime."""

    from network.validation.component_profiles import (  # noqa: PLC0415
        COMPONENT_PYTHON_MODULES,
        load_profiles,
        validate_component_python_runtime,
    )

    dependencies = dependency_versions if isinstance(dependency_versions, dict) else {}
    record = dependencies.get("python_runtime")
    profile = load_profiles().get(qualification_profile)
    if profile is None:
        return (
            []
            if record is None
            else ["non-component provenance recorded an undeclared Python runtime"]
        )
    failures = validate_component_python_runtime(profile, record, dependencies)
    if failures or profile["python_runtime"] != "sionna_rt_cuda":
        return failures

    # The independent validator runs in the same immutable image as the
    # producer.  Re-hash every recorded origin and resolve the actual modules
    # here so producer-authored provenance cannot substitute another file.
    executable = record["executable"]
    executable_path = Path(executable["realpath"])
    try:
        current_executable = Path(sys.executable).resolve(strict=True)
        executable_stat = executable_path.stat()
    except OSError as exc:
        failures.append(f"component Python executable could not be re-observed: {exc}")
    else:
        if (
            current_executable != executable_path
            or not executable_path.is_file()
            or executable_stat.st_size != executable["size_bytes"]
            or sha256_file(executable_path) != executable["sha256"]
        ):
            failures.append("component Python executable differs in the exact image")
    for module_name, distribution in COMPONENT_PYTHON_MODULES.items():
        module = record["modules"][module_name]
        try:
            spec = importlib.util.find_spec(module_name)
            if spec is None or not isinstance(spec.origin, str):
                raise ValueError("module spec/origin is unavailable")
            actual_origin = Path(spec.origin).resolve(strict=True)
            recorded_origin = Path(module["origin"])
            origin_stat = actual_origin.stat()
            actual_version = metadata.version(distribution)
        except (OSError, ValueError, ImportError, metadata.PackageNotFoundError) as exc:
            failures.append(
                f"component Python module could not be re-observed: {module_name}: {exc}"
            )
            continue
        if (
            actual_origin != recorded_origin
            or not actual_origin.is_file()
            or origin_stat.st_size != module["size_bytes"]
            or sha256_file(actual_origin) != module["sha256"]
            or actual_version != module["version"]
        ):
            failures.append(
                f"component Python module differs in the exact image: {module_name}"
            )
    return failures


def finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def nonnegative_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _parse_ip_packet(frame: bytes, offset: int, version: int, stats: dict[str, Any], nonce: bytes | None) -> None:
    if version == 4:
        if len(frame) < offset + 20:
            stats["truncated_packets"] += 1
            return
        ihl = (frame[offset] & 0x0F) * 4
        if ihl < 20 or len(frame) < offset + ihl:
            stats["truncated_packets"] += 1
            return
        protocol = frame[offset + 9]
        transport_offset = offset + ihl
        total_length = struct.unpack("!H", frame[offset + 2 : offset + 4])[0]
        if total_length < ihl or len(frame) < offset + total_length:
            stats["truncated_packets"] += 1
            return
        packet_end = offset + total_length
        stats["ipv4_packets"] += 1
    elif version == 6:
        if len(frame) < offset + 40:
            stats["truncated_packets"] += 1
            return
        protocol = frame[offset + 6]
        transport_offset = offset + 40
        payload_length = struct.unpack("!H", frame[offset + 4 : offset + 6])[0]
        packet_end = offset + 40 + payload_length
        if len(frame) < packet_end:
            stats["truncated_packets"] += 1
            return
        stats["ipv6_packets"] += 1
    else:
        stats["other_packets"] += 1
        return

    if protocol == 17 and packet_end >= transport_offset + 8:
        src_port, dst_port = struct.unpack("!HH", frame[transport_offset : transport_offset + 4])
        udp_length = struct.unpack("!H", frame[transport_offset + 4 : transport_offset + 6])[0]
        if udp_length < 8 or transport_offset + udp_length > packet_end:
            stats["truncated_packets"] += 1
            return
        stats["udp_packets"] += 1
        stats["ports"].add(src_port)
        stats["ports"].add(dst_port)
        payload = frame[transport_offset + 8 : transport_offset + udp_length]
    elif protocol == 6 and packet_end >= transport_offset + 20:
        src_port, dst_port = struct.unpack("!HH", frame[transport_offset : transport_offset + 4])
        data_offset = ((frame[transport_offset + 12] >> 4) & 0x0F) * 4
        if data_offset < 20 or transport_offset + data_offset > packet_end:
            stats["truncated_packets"] += 1
            return
        stats["tcp_packets"] += 1
        stats["ports"].add(src_port)
        stats["ports"].add(dst_port)
        payload = frame[transport_offset + data_offset : packet_end]
    else:
        stats["other_ip_packets"] += 1
        payload = frame[transport_offset:packet_end]

    if protocol in (6, 17) and payload:
        payload_hash = hashlib.sha256(payload).hexdigest()
        stats["payload_sha256"].add(payload_hash)
        if nonce and nonce in payload:
            stats["payload_sha256_with_nonce"].add(payload_hash)
    if nonce and nonce in payload:
        stats["nonce_hits"] += 1


def _parse_link_frame(frame: bytes, linktype: int, stats: dict[str, Any], nonce: bytes | None) -> None:
    protocol: int | None = None
    offset = 0

    if linktype == 1:  # DLT_EN10MB
        if len(frame) < 14:
            stats["truncated_packets"] += 1
            return
        protocol = struct.unpack("!H", frame[12:14])[0]
        offset = 14
        while protocol in (0x8100, 0x88A8, 0x9100) and len(frame) >= offset + 4:
            protocol = struct.unpack("!H", frame[offset + 2 : offset + 4])[0]
            offset += 4
    elif linktype == 113:  # DLT_LINUX_SLL
        if len(frame) < 16:
            stats["truncated_packets"] += 1
            return
        protocol = struct.unpack("!H", frame[14:16])[0]
        offset = 16
    elif linktype == 276:  # DLT_LINUX_SLL2
        if len(frame) < 20:
            stats["truncated_packets"] += 1
            return
        protocol = struct.unpack("!H", frame[0:2])[0]
        offset = 20
    elif linktype == 101:  # DLT_RAW
        if not frame:
            stats["truncated_packets"] += 1
            return
        version = frame[0] >> 4
        _parse_ip_packet(frame, 0, version, stats, nonce)
        return
    elif linktype == 0:  # DLT_NULL / loopback
        if len(frame) < 4:
            stats["truncated_packets"] += 1
            return
        family_le = struct.unpack("<I", frame[:4])[0]
        family_be = struct.unpack(">I", frame[:4])[0]
        family = family_le if family_le in (2, 10, 24, 28, 30) else family_be
        _parse_ip_packet(frame, 4, 4 if family == 2 else 6, stats, nonce)
        return
    else:
        stats["unsupported_linktype"] = True
        return

    if protocol == 0x0806:
        stats["arp_packets"] += 1
    elif protocol == 0x0800:
        _parse_ip_packet(frame, offset, 4, stats, nonce)
    elif protocol == 0x86DD:
        _parse_ip_packet(frame, offset, 6, stats, nonce)
    else:
        stats["other_packets"] += 1


def pcap_stats(path: Path, nonce: str | None = None) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "path": str(path),
        "present": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": sha256_file(path),
        "total_packets": 0,
        "arp_packets": 0,
        "ipv4_packets": 0,
        "ipv6_packets": 0,
        "udp_packets": 0,
        "tcp_packets": 0,
        "other_ip_packets": 0,
        "other_packets": 0,
        "truncated_packets": 0,
        "nonce_hits": 0,
        "payload_sha256": set(),
        "payload_sha256_with_nonce": set(),
        "ports": set(),
        "unsupported_linktype": False,
        "parse_error": None,
    }
    if not path.is_file():
        stats["ports"] = []
        stats["payload_sha256"] = []
        stats["payload_sha256_with_nonce"] = []
        return stats

    nonce_bytes = nonce.encode("utf-8") if nonce else None
    try:
        with path.open("rb") as handle:
            global_header = handle.read(24)
            if len(global_header) < 24:
                stats["parse_error"] = "missing classic-PCAP global header"
                stats["ports"] = []
                stats["payload_sha256"] = []
                stats["payload_sha256_with_nonce"] = []
                return stats
            magic = global_header[:4]
            if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
                endian = "<"
            elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
                endian = ">"
            elif magic == b"\x0a\x0d\x0d\x0a":
                stats["parse_error"] = "pcapng is not supported by the built-in parser"
                stats["ports"] = []
                stats["payload_sha256"] = []
                stats["payload_sha256_with_nonce"] = []
                return stats
            else:
                stats["parse_error"] = f"unknown PCAP magic {magic.hex()}"
                stats["ports"] = []
                stats["payload_sha256"] = []
                stats["payload_sha256_with_nonce"] = []
                return stats
            linktype = struct.unpack(endian + "I", global_header[20:24])[0]
            stats["linktype"] = linktype
            while True:
                packet_header = handle.read(16)
                if not packet_header:
                    break
                if len(packet_header) != 16:
                    stats["truncated_packets"] += 1
                    break
                _ts_sec, _ts_fraction, captured_len, _original_len = struct.unpack(
                    endian + "IIII", packet_header
                )
                if captured_len > 64 * 1024 * 1024:
                    stats["parse_error"] = f"implausible captured length {captured_len}"
                    break
                frame = handle.read(captured_len)
                if len(frame) != captured_len:
                    stats["truncated_packets"] += 1
                    break
                stats["total_packets"] += 1
                _parse_link_frame(frame, linktype, stats, nonce_bytes)
    except OSError as exc:
        stats["parse_error"] = str(exc)
    stats["ports"] = sorted(stats["ports"])
    stats["payload_sha256"] = sorted(stats["payload_sha256"])
    stats["payload_sha256_with_nonce"] = sorted(stats["payload_sha256_with_nonce"])
    stats["data_packets"] = stats["udp_packets"] + stats["tcp_packets"]
    return stats


def inspect_class_pcaps(run_dir: Path, nonce: str | None = None) -> dict[str, Any]:
    class_stats = {
        name: pcap_stats(run_dir / "pcap" / f"{name}.pcap", nonce=nonce)
        for name in TRAFFIC_CLASSES
    }
    ns3_hashes: dict[str, list[str]] = {}
    for path in sorted((run_dir / "pcap").glob("ns3-p2mp-*.pcap")):
        digest = sha256_file(path)
        if digest:
            ns3_hashes.setdefault(digest, []).append(path.name)

    failures: list[str] = []
    for traffic_class, stats in class_stats.items():
        if not stats["present"]:
            failures.append(f"{traffic_class}: PCAP missing")
            continue
        if stats["parse_error"]:
            failures.append(f"{traffic_class}: {stats['parse_error']}")
        if stats["truncated_packets"]:
            failures.append(
                f"{traffic_class}: capture contains {stats['truncated_packets']} truncated packet(s)"
            )
        if stats["unsupported_linktype"]:
            failures.append(f"{traffic_class}: unsupported PCAP link type")
        if stats["data_packets"] <= 0:
            failures.append(
                f"{traffic_class}: no UDP/TCP data packets (total={stats['total_packets']}, arp={stats['arp_packets']})"
            )
        digest = stats.get("sha256")
        if digest and digest in ns3_hashes:
            failures.append(
                f"{traffic_class}: byte-identical to generic ns-3 capture(s) {', '.join(ns3_hashes[digest])}"
            )
        if nonce and stats["nonce_hits"] <= 0:
            failures.append(f"{traffic_class}: run nonce not found")

    return {
        "passed": not failures,
        "failures": failures,
        "classes": class_stats,
    }


def delivery_status(summary: dict[str, Any]) -> dict[str, Any]:
    packets = summary.get("packets") if isinstance(summary.get("packets"), dict) else {}
    loss = summary.get("loss_rate") if isinstance(summary.get("loss_rate"), dict) else {}
    latency = summary.get("latency_ms") if isinstance(summary.get("latency_ms"), dict) else {}
    failures: list[str] = []
    per_class: dict[str, Any] = {}
    for traffic_class, (tx_key, rx_key) in PACKET_KEYS.items():
        tx = packets.get(tx_key)
        rx = packets.get(rx_key)
        loss_value = loss.get(traffic_class)
        per_class[traffic_class] = {"tx": tx, "rx": rx, "loss": loss_value}
        if not nonnegative_integer(tx) or tx <= 0:
            failures.append(f"{traffic_class}: transmitted packet count is not positive")
        if not nonnegative_integer(rx) or rx <= 0:
            failures.append(f"{traffic_class}: received packet count is not positive")
        if nonnegative_integer(tx) and nonnegative_integer(rx) and rx > tx:
            failures.append(f"{traffic_class}: received packet count exceeds transmitted count")
        if not finite_number(loss_value):
            failures.append(f"{traffic_class}: loss metric is missing")
        else:
            numeric_loss = float(loss_value)
            if not 0.0 <= numeric_loss <= 1.0:
                failures.append(f"{traffic_class}: loss metric is outside [0, 1]")
            elif numeric_loss >= 1.0:
                failures.append(f"{traffic_class}: complete packet loss")
            if nonnegative_integer(tx) and tx > 0 and nonnegative_integer(rx) and rx <= tx:
                expected_loss = (tx - rx) / tx
                if not math.isclose(numeric_loss, expected_loss, rel_tol=1e-9, abs_tol=1e-9):
                    failures.append(
                        f"{traffic_class}: loss {numeric_loss} is inconsistent with TX/RX {tx}/{rx}"
                    )
    for key in REQUIRED_LATENCY_KEYS:
        value = latency.get(key)
        if not finite_number(value):
            failures.append(f"latency_ms.{key} is missing")
        elif float(value) < 0:
            failures.append(f"latency_ms.{key} is negative")
    for traffic_class in ("control", "payload"):
        p50 = latency.get(f"{traffic_class}_p50")
        p95 = latency.get(f"{traffic_class}_p95")
        if finite_number(p50) and finite_number(p95) and float(p95) < float(p50):
            failures.append(f"{traffic_class}: p95 latency is below p50")
    return {
        "passed": not failures,
        "failures": failures,
        "traffic_classes": per_class,
        "latency_ms": {key: latency.get(key) for key in REQUIRED_LATENCY_KEYS},
    }


def critical_log_findings(run_dir: Path) -> list[str]:
    findings: list[str] = []
    candidates = sorted((run_dir / "logs").glob("*.log"))
    for path in candidates:
        try:
            text = path.read_text(errors="replace").lower()
        except OSError:
            continue
        for pattern in CRITICAL_LOG_PATTERNS:
            if pattern in text:
                findings.append(f"logs/{path.name} contains {pattern!r}")
    return findings


def dependency_status(run_dir: Path) -> dict[str, Any]:
    rc = read_exit_code(run_dir / "logs" / "check_deps.log.exit_code")
    log_path = run_dir / "logs" / "check_deps.log"
    findings = critical_log_findings(run_dir)
    if rc != 0:
        return gate("failed" if rc is not None else "not_run", f"dependency check exit code is {rc}")
    if not log_path.is_file() or log_path.stat().st_size == 0:
        return gate("not_run", "dependency check log is missing")
    capture_logs = [run_dir / "logs" / f"tcpdump_{name}.log" for name in TRAFFIC_CLASSES]
    missing_capture_logs = [path.name for path in capture_logs if not path.is_file() or path.stat().st_size == 0]
    if missing_capture_logs:
        return gate(
            "failed",
            "runtime capture logs are missing or empty",
            {"missing": missing_capture_logs},
        )
    if findings:
        return gate("failed", "runtime capture capability failed despite dependency check", {"findings": findings})
    return gate("passed", "dependency check exited zero and no capture permission failure was found")


def _structured_file(run_dir: Path, relative: str) -> tuple[Path, dict[str, Any]]:
    path = run_dir / relative
    return path, load_json(path)


def checked_run_file(run_dir: Path, relative: Any) -> tuple[Path | None, str | None]:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None, "raw evidence path is missing or absolute"
    path = (run_dir / relative).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        return None, "raw evidence path escapes the run directory"
    if not path.is_file():
        return None, f"raw evidence file is missing: {relative}"
    return path, None


def sealed_regular_file(
    run_dir: Path, relative: Any
) -> tuple[Path | None, os.stat_result | None, str | None]:
    """Resolve one sealed file without following aliases outside its lexical path."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None, None, "sealed evidence path is missing or invalid"
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(part in ("", ".", "..") for part in relative_path.parts):
        return None, None, f"sealed evidence path is not a canonical relative path: {relative!r}"
    base = run_dir.resolve()
    current = base
    try:
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                return None, None, f"sealed evidence path has a symbolic-link component: {relative}"
        resolved = current.resolve(strict=True)
        resolved.relative_to(base)
        file_stat = current.stat(follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, None, f"sealed evidence file is missing or invalid: {relative}: {exc}"
    if not stat.S_ISREG(file_stat.st_mode):
        return None, None, f"sealed evidence path is not a regular file: {relative}"
    if file_stat.st_nlink != 1:
        return (
            current,
            file_stat,
            f"sealed evidence file has {file_stat.st_nlink} hard links: {relative}",
        )
    return current, file_stat, None


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return [], [f"could not read {path.name}: {exc}"]
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f"{path.name}:{line_number} is not valid JSON")
            continue
        if not isinstance(record, dict):
            failures.append(f"{path.name}:{line_number} is not a JSON object")
            continue
        records.append(record)
    return records, failures


def raw_event_envelope_failures(
    records: list[dict[str, Any]],
    *,
    run_id: str,
    runtime_id: Any,
    source_hash: Any,
) -> list[str]:
    """Validate the immutable identity/order envelope on every raw event."""
    failures: list[str] = []
    previous_clock: int | None = None
    if not records:
        return ["raw event log is empty"]
    for index, record in enumerate(records, start=1):
        context = f"raw[{index - 1}]"
        for key, expected in (
            ("schema_version", 2),
            ("run_id", run_id),
            ("runtime_id", runtime_id),
            ("source_hash", source_hash),
            ("event_seq", index),
        ):
            if record.get(key) != expected:
                failures.append(f"{context}.{key} does not match the raw-event envelope")
        event = record.get("event")
        if not isinstance(event, str) or not event:
            failures.append(f"{context}.event is not a non-empty string")
        clock = record.get("monotonic_ns")
        if not nonnegative_integer(clock) or (
            previous_clock is not None and clock <= previous_clock
        ):
            failures.append(f"{context}.monotonic_ns is not strictly increasing")
        elif isinstance(clock, int):
            previous_clock = clock
        wall_utc = record.get("wall_utc")
        try:
            parsed_wall = datetime.fromisoformat(str(wall_utc).replace("Z", "+00:00"))
            if parsed_wall.tzinfo is None:
                raise ValueError("timezone is missing")
        except (TypeError, ValueError):
            failures.append(f"{context}.wall_utc is not a timezone-aware timestamp")
    return failures


def sequence_metrics_ns(values: list[Any]) -> dict[str, float] | None:
    if not values or any(not nonnegative_integer(value) for value in values):
        return None
    if any(current <= previous for previous, current in zip(values, values[1:])):
        return None
    span_s = (values[-1] - values[0]) / 1_000_000_000
    return {
        "first_ns": float(values[0]),
        "last_ns": float(values[-1]),
        "max_gap_s": max(
            ((current - previous) / 1_000_000_000 for previous, current in zip(values, values[1:])),
            default=0.0,
        ),
        "rate_hz": (len(values) - 1) / span_s if len(values) >= 2 and span_s > 0 else 0.0,
    }


def provenance_status(run_dir: Path) -> dict[str, Any]:
    path, data = _structured_file(run_dir, "metrics/provenance.json")
    required = (
        "run_id",
        "git_commit",
        "git_dirty",
        "source_hash",
        "config_hashes",
        "dependency_versions",
        "container_image",
    )
    missing = [key for key in required if key not in data]
    if not data:
        return gate("not_run", f"{path.relative_to(run_dir)} is missing or invalid")
    if missing:
        return gate("failed", "provenance record is incomplete", {"missing": missing})
    if str(data.get("run_id")) != run_dir.name:
        return gate("failed", "provenance run_id does not match run directory")
    failures: list[str] = []
    if data.get("schema_version") != 2:
        failures.append("provenance schema_version is not 2")
    try:
        expected_plan_contract = {
            "plan_version": 3,
            "path": "doc/network_radio_integration_plan_v3.md",
            "contract_sha256": sha256_file(
                ROOT_DIR / "doc/network_radio_integration_plan_v3.md"
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        expected_plan_contract = None
        failures.append(f"authoritative v3 plan could not be hashed: {exc}")
    if expected_plan_contract is None or data.get("plan_contract") != expected_plan_contract:
        failures.append("provenance plan_version/contract hash is not exact")
    if not isinstance(data.get("generated_utc"), str):
        failures.append("generated_utc is missing")
    else:
        try:
            datetime.fromisoformat(data["generated_utc"].replace("Z", "+00:00"))
        except ValueError:
            failures.append("generated_utc is invalid")
    commit = data.get("git_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        failures.append("git_commit is not a full hexadecimal commit")
    source_hash = data.get("source_hash")
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        failures.append("source_hash is not SHA-256")
    if not nonnegative_integer(data.get("source_files")) or data.get("source_files", 0) < 1:
        failures.append("source_files is invalid")
    if data.get("git_dirty") is not False:
        failures.append("source checkout is dirty")
    if data.get("git_status") != []:
        failures.append("git_status is not empty")
    clean_diff_hash = hashlib.sha256(b"").hexdigest()
    if data.get("git_diff_sha256") != clean_diff_hash:
        failures.append("git_diff_sha256 is missing or not the clean-tree digest")
    if data.get("dependency_lock_status") != "complete":
        failures.append("recorded dependency-lock status is not complete")
    if data.get("acceptance_blockers") != []:
        failures.append("provenance contains acceptance blockers")
    config_hashes = data.get("config_hashes") if isinstance(data.get("config_hashes"), dict) else {}
    if not config_hashes:
        failures.append("config_hashes is empty")
    for relative, expected_hash in config_hashes.items():
        try:
            if not isinstance(relative, str) or not relative or "\x00" in relative:
                raise ValueError("path is not a non-empty filesystem string")
            config_path = (ROOT_DIR / relative).resolve()
            config_path.relative_to(ROOT_DIR)
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(f"config hash path is invalid or escapes source root: {relative!r}: {exc}")
            continue
        try:
            actual_hash = sha256_file(config_path)
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(f"config hash path is unreadable: {relative!r}: {exc}")
            continue
        if not isinstance(expected_hash, str) or expected_hash != actual_hash:
            failures.append(f"config hash mismatch: {relative}")
    container = data.get("container_image") if isinstance(data.get("container_image"), dict) else {}
    if not isinstance(container.get("reference"), str) or not container.get("reference"):
        failures.append("container image reference is missing")
    if not isinstance(container.get("digest"), str) or re.fullmatch(
        r"sha256:[0-9a-f]{64}", container.get("digest", "")
    ) is None:
        failures.append("container image digest is not pinned")
    if container.get("digest_source") != "docker_image_inspect_host":
        failures.append("container image digest lacks host docker-inspect attestation")
    if not isinstance(container.get("runtime_container_id"), str) or re.fullmatch(
        r"[0-9a-f]{64}", container.get("runtime_container_id", "")
    ) is None:
        failures.append("full runtime container ID is missing or invalid")
    if container.get("runtime_container_id_source") != "host_bind_mount":
        failures.append("runtime container ID lacks host bind-mount provenance")
    dependencies = data.get("dependency_versions") if isinstance(data.get("dependency_versions"), dict) else {}
    for name in (
        "python",
        "ros_distribution",
        "gazebo",
        "sionna-rt",
        "mitsuba",
        "numpy",
        "PyYAML",
        "matplotlib",
        "pymavlink",
        "ns3",
        "external_sources",
        "runtime_manifests",
        "runtime_capabilities",
    ):
        if name not in dependencies:
            failures.append(f"dependency version is missing: {name}")
    qualification = data.get("qualification_consumption")
    qualification_profile = (
        qualification.get("profile") if isinstance(qualification, dict) else None
    )
    failures.extend(
        component_python_runtime_failures(qualification_profile, dependencies)
    )
    ns3 = dependencies.get("ns3") if isinstance(dependencies.get("ns3"), dict) else {}
    if (
        ns3.get("source_kind") != "official_release_archive"
        or ns3.get("version") != "3.40"
        or ns3.get("archive_sha256")
        != "c0ba395b6fcb084c4d43d6117b28932f716b26aebb54498ce2f44c0c39be3e60"
        or ns3.get("core_tree_sha256")
        != "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
        or ns3.get("source_clean") is not True
    ):
        failures.append("accepted ns-3 release source does not match the pinned archive/tree")
    lock_path = ROOT_DIR / "network/config/dependency_lock.yaml"
    try:
        loaded_dependency_lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(f"dependency lock could not be loaded: {exc}")
        dependency_lock = {}
    else:
        if isinstance(loaded_dependency_lock, dict):
            dependency_lock = loaded_dependency_lock
        else:
            failures.append("dependency lock root is not a mapping")
            dependency_lock = {}

    def lock_mapping(value: Any, label: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        failures.append(f"dependency lock field is not a mapping: {label}")
        return {}

    if dependency_lock.get("schema_version") != 2 or dependency_lock.get("status") != "complete":
        failures.append("dependency lock is not complete")
    lock_dependencies = lock_mapping(
        dependency_lock.get("dependencies"), "dependencies"
    )
    expected_packages = lock_mapping(
        lock_dependencies.get("python_packages"), "dependencies.python_packages"
    )
    for package, expected_version in expected_packages.items():
        if dependencies.get(package) != str(expected_version):
            failures.append(
                f"dependency {package}={dependencies.get(package)!r} does not match lock {expected_version!r}"
            )
    external_sources = (
        dependencies.get("external_sources")
        if isinstance(dependencies.get("external_sources"), dict)
        else {}
    )
    ros2_repos = lock_mapping(
        lock_dependencies.get("ardupilot_ros_repos"),
        "dependencies.ardupilot_ros_repos",
    )
    ros2_revisions = lock_mapping(
        ros2_repos.get("revisions"),
        "dependencies.ardupilot_ros_repos.revisions",
    )
    gz_repos = lock_mapping(
        lock_dependencies.get("ardupilot_gz_repos"),
        "dependencies.ardupilot_gz_repos",
    )
    gz_revisions = lock_mapping(
        gz_repos.get("revisions"),
        "dependencies.ardupilot_gz_repos.revisions",
    )
    ardupilot_lock = lock_mapping(
        lock_dependencies.get("ardupilot"), "dependencies.ardupilot"
    )
    micro_xrce_lock = lock_mapping(
        lock_dependencies.get("micro_xrce_dds_gen"),
        "dependencies.micro_xrce_dds_gen",
    )
    expected_external_sources = {
        "ardupilot_standalone": ardupilot_lock.get("revision"),
        "ardupilot_ros2": ros2_revisions.get("ardupilot"),
        "micro_ros_agent": ros2_revisions.get("micro_ros_agent"),
        "ardupilot_gazebo": gz_revisions.get("ardupilot_gazebo"),
        "ardupilot_gz": gz_revisions.get("ardupilot_gz"),
        "ardupilot_sitl_models": gz_revisions.get("ardupilot_sitl_models"),
        "ros_gz": gz_revisions.get("ros_gz"),
        "sdformat_urdf": gz_revisions.get("sdformat_urdf"),
        "micro_xrce_dds_gen": micro_xrce_lock.get("revision"),
    }
    from network.scripts.write_run_provenance import (
        CANONICAL_RUNTIME_SOURCE_PATHS,
        runtime_manifest_commands,
    )

    for name, expected_commit in expected_external_sources.items():
        record = external_sources.get(name) if isinstance(external_sources.get(name), dict) else {}
        if (
            re.fullmatch(r"[0-9a-f]{40}", str(expected_commit or "")) is None
            or record.get("is_git_checkout") is not True
            or record.get("commit") != expected_commit
            or record.get("dirty") is not False
            or record.get("path") != CANONICAL_RUNTIME_SOURCE_PATHS[name]
        ):
            failures.append(f"external source {name} does not match its clean locked commit")
    if dependencies.get("ros_distribution") != "humble":
        failures.append("recorded ROS distribution is not humble")
    gazebo_lock = lock_mapping(lock_dependencies.get("gazebo"), "dependencies.gazebo")
    if dependencies.get("gazebo") != str(gazebo_lock.get("version")):
        failures.append("recorded Gazebo version does not match dependency lock")
    runtime_policy = lock_mapping(
        dependency_lock.get("runtime_policy"), "runtime_policy"
    )
    capabilities = (
        dependencies.get("runtime_capabilities")
        if isinstance(dependencies.get("runtime_capabilities"), dict)
        else {}
    )
    if capabilities.get("system") != runtime_policy.get("system"):
        failures.append("runtime operating system does not match dependency lock")
    if capabilities.get("machine") != runtime_policy.get("machine"):
        failures.append("runtime machine architecture does not match dependency lock")
    if capabilities.get("mitsuba_variant") != runtime_policy.get("mitsuba_variant"):
        failures.append("Mitsuba variant does not match dependency lock")
    if not isinstance(capabilities.get("kernel_release"), str) or not capabilities.get(
        "kernel_release"
    ):
        failures.append("runtime kernel release is missing")
    if not isinstance(capabilities.get("kernel_version"), str) or not capabilities.get(
        "kernel_version"
    ):
        failures.append("runtime kernel version is missing")
    host_identity = (
        capabilities.get("host") if isinstance(capabilities.get("host"), dict) else {}
    )
    if re.fullmatch(r"[0-9a-f]{64}", str(host_identity.get("boot_id_sha256") or "")) is None:
        failures.append("runtime host boot identity is missing")
    if re.fullmatch(r"[0-9a-f]{64}", str(host_identity.get("machine_id_sha256") or "")) is None:
        failures.append("runtime host machine identity is missing")
    gpu = capabilities.get("gpu") if isinstance(capabilities.get("gpu"), dict) else {}
    if not isinstance(gpu.get("available"), bool) or not isinstance(gpu.get("devices"), list):
        failures.append("GPU/runtime record is malformed")
    if runtime_policy.get("gpu_required") is True and gpu.get("available") is not True:
        failures.append("dependency lock requires a visible GPU")
    expected_network = lock_mapping(
        runtime_policy.get("required_network_capabilities"),
        "runtime_policy.required_network_capabilities",
    )
    observed_network = (
        capabilities.get("network")
        if isinstance(capabilities.get("network"), dict)
        else {}
    )
    from network.validation.qualification_identity import (
        BOUNDED_ROOT_IN_RUNTIME_MODE,
        BOUNDED_ROOT_IN_RUNTIME_PROFILES,
        DEFERRED_M0_CAPABILITY_MODE,
        is_exact_deferred_m0_capability_mode,
        qualification_prefixes_equal,
    )

    qualification_mode = observed_network.get("qualification_mode")
    deferred_m0_capabilities = is_exact_deferred_m0_capability_mode(
        data.get("qualification_consumption"), qualification_mode
    )
    (
        bounded_root_capabilities,
        recorded_bounded_root_exact,
        independently_observed_bounded_root_exact,
        _,
    ) = _bounded_root_runtime_evidence(
        data.get("qualification_consumption"), observed_network
    )
    inherited_m1_capabilities = False
    if qualification_mode == "inherited_m0_host_final":
        inherited = data.get("inherited_m0_qualification")
        try:
            consumption = data.get("qualification_consumption")
            inherited_profile = (
                consumption.get("profile") if isinstance(consumption, dict) else None
            )
            receipt_path = INHERITED_M0_RECEIPT_MOUNTS.get(inherited_profile)
            if receipt_path is None:
                raise ValueError("inherited M0 receipt profile has no canonical mount")
            info = receipt_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size < 2
                or info.st_size > 64 * 1024 * 1024
                or info.st_mode & 0o222
            ):
                raise ValueError("mounted M0 receipt is not bounded/read-only")
            receipt_raw = receipt_path.read_bytes()
            after = receipt_path.lstat()
            if (
                len(receipt_raw) != info.st_size
                or (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError("mounted M0 receipt changed while read")
            receipt = json.loads(
                receipt_raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite M0 receipt JSON: {value}")
                ),
            )
            if receipt_raw != (
                json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"):
                raise ValueError("mounted M0 receipt is not canonical JSON")
            m0_vector = receipt.get("qualification_content_vector")
            current_vector = data.get("qualification_content_vector")
            host_final = receipt.get("gates", {}).get("host_final", {})
            capability = (
                host_final.get("details", {}).get("isolated_target_runtime_capability")
                if isinstance(host_final, dict)
                else None
            )
            receipt_hash = hashlib.sha256(receipt_raw).hexdigest()
            if (
                not isinstance(inherited, dict)
                or inherited.get("schema_version") != 1
                or inherited.get("contract")
                != "ams.m1.inherited-m0-qualification/v1"
                or inherited.get("status_report_commit") != data.get("git_commit")
                or inherited.get("mounted_receipt_path") != str(receipt_path)
                or inherited.get("receipt_sha256") != receipt_hash
                or inherited.get("receipt_contract")
                != "ams.m0.host-final-receipt/v1"
                or inherited.get("receipt_run_id") != receipt.get("run_id")
                or inherited.get("qualification_contract_sha256")
                != receipt.get("qualification_contract_sha256")
                or inherited.get("qualification_vector_sha256")
                != m0_vector.get("vector_sha256")
                or inherited.get("qualification_vector_commit")
                != m0_vector.get("git_commit")
                or inherited.get("image_digest") != container.get("digest")
                or inherited.get("consumed_nodes") != ["Q0"]
                or inherited.get("available") is not True
                or receipt.get("schema_version") != 3
                or receipt.get("contract") != "ams.m0.host-final-receipt/v1"
                or receipt.get("milestone") != "M0"
                or receipt.get("receipt_path")
                != inherited.get("canonical_receipt_path")
                or receipt.get("formal_accepted") is not True
                or receipt.get("passed") is not True
                or receipt.get("failures") != []
                or receipt.get("consumed_nodes") != ["Q0"]
                or not qualification_prefixes_equal(
                    m0_vector, current_vector, ["Q0"]
                )
                or not isinstance(capability, dict)
                or capability.get("contract")
                != "ams.m0.isolated-capability-probe/v1"
                or capability.get("image_digest") != container.get("digest")
                or capability.get("exit_code") != 0
                or capability.get("no_candidate_mounts") is not True
                or capability.get("tun_device") is not True
                or capability.get("passwordless_sudo") is not True
                or capability.get("unshare_network_namespace") is not True
            ):
                raise ValueError("inherited M0/Q0 receipt binding is not exact")
            inherited_m1_capabilities = True
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            failures.append(f"inherited M0 capability qualification is invalid: {exc}")
    if qualification_mode not in {
        "in_runtime",
        DEFERRED_M0_CAPABILITY_MODE,
        "inherited_m0_host_final",
        BOUNDED_ROOT_IN_RUNTIME_MODE,
    }:
        failures.append("network capability qualification mode is invalid")
    elif (
        qualification_mode == DEFERRED_M0_CAPABILITY_MODE
        and not deferred_m0_capabilities
    ):
        failures.append(
            "isolated host-final capability qualification is allowed only for M0/Q0"
        )
    elif qualification_mode == "inherited_m0_host_final" and not inherited_m1_capabilities:
        failures.append(
            "inherited host-final capability qualification is allowed only for exact M1/M0"
        )
    elif qualification_mode == BOUNDED_ROOT_IN_RUNTIME_MODE and (
        not bounded_root_capabilities
        or not recorded_bounded_root_exact
        or not independently_observed_bounded_root_exact
    ):
        failures.append(
            "bounded-root capability record/current validator runtime is not the exact "
            "M2--M4 uid/gid/capability/no-new-privileges contract"
        )
    else:
        consumption = data.get("qualification_consumption")
        capability_profile = (
            consumption.get("profile") if isinstance(consumption, dict) else None
        )
        if (
            capability_profile in BOUNDED_ROOT_IN_RUNTIME_PROFILES
            and qualification_mode != BOUNDED_ROOT_IN_RUNTIME_MODE
        ):
            failures.append(
                "M2--M4 TUN qualification requires bounded_root_in_runtime mode"
            )
    for capability, required in expected_network.items():
        if (
            required is True
            and observed_network.get(capability) is not True
            and not deferred_m0_capabilities
            and not inherited_m1_capabilities
            and not (
                bounded_root_capabilities
                and recorded_bounded_root_exact
                and independently_observed_bounded_root_exact
                and capability == "passwordless_sudo"
            )
        ):
            failures.append(f"required network capability is unavailable: {capability}")
    runtime_manifests = (
        dependencies.get("runtime_manifests")
        if isinstance(dependencies.get("runtime_manifests"), dict)
        else {}
    )
    expected_runtime_manifests = lock_mapping(
        dependency_lock.get("runtime_manifest_sha256"), "runtime_manifest_sha256"
    )
    expected_manifest_commands = runtime_manifest_commands()
    for manifest_name in ("pip_freeze", "dpkg", "ros_packages"):
        manifest = (
            runtime_manifests.get(manifest_name)
            if isinstance(runtime_manifests.get(manifest_name), dict)
            else {}
        )
        lines = manifest.get("lines")
        valid_lines = isinstance(lines, list) and all(isinstance(line, str) for line in lines)
        normalized = (
            "\n".join(lines) + ("\n" if lines else "")
            if valid_lines
            else None
        )
        if (
            manifest.get("command") != expected_manifest_commands[manifest_name]
            or
            manifest.get("available") is not True
            or not nonnegative_integer(manifest.get("entries"))
            or manifest.get("entries", 0) < 1
            or not isinstance(manifest.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256", "")) is None
            or not valid_lines
            or (valid_lines and lines != sorted(set(lines)))
            or len(lines) != manifest.get("entries")
            or normalized is None
            or hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            != manifest.get("sha256")
        ):
            failures.append(f"runtime dependency manifest is invalid: {manifest_name}")
        expected_manifest_hash = expected_runtime_manifests.get(manifest_name)
        if manifest.get("sha256") != expected_manifest_hash:
            failures.append(f"runtime dependency manifest does not match lock: {manifest_name}")
    accepted_path = (
        dependency_lock.get("accepted_p0_path")
        if isinstance(dependency_lock.get("accepted_p0_path"), dict)
        else {}
    )
    implementation = data.get("implementation") if isinstance(data.get("implementation"), dict) else {}
    from network.validation.component_profiles import (  # noqa: PLC0415
        expected_radio_provider_runtime,
    )

    expected_implementation = {
        "packet_ingress_mode": accepted_path.get("packet_ingress"),
        "medium_model": accepted_path.get("medium_model"),
        "radio_provider_id": accepted_path.get("radio_provider"),
        **expected_radio_provider_runtime(
            qualification_profile, accepted_path.get("radio_provider")
        ),
    }
    if implementation != expected_implementation:
        failures.append("runtime implementation does not match the accepted dependency-lock path")
    ros_lock = lock_mapping(lock_dependencies.get("ros"), "dependencies.ros")
    if container.get("reference") != ros_lock.get("project_image_reference"):
        failures.append("container image reference does not match dependency lock")
    if container.get("digest") != ros_lock.get("project_image_digest"):
        failures.append("container image digest does not match dependency lock")
    try:
        from network.scripts.write_run_provenance import (
            default_configs_for_profile,
            deterministic_source_hash,
            source_files_for_profile,
        )
        from network.validation.qualification_identity import (
            validate_recorded_checkout_identity,
            verify_recorded_qualification,
        )

        qualification_relationship = verify_recorded_qualification(
            ROOT_DIR,
            data.get("qualification_content_vector"),
            data.get("qualification_consumption"),
        )
        validate_recorded_checkout_identity(
            data.get("qualification_checkout"),
            data.get("qualification_content_vector"),
        )
        if qualification_relationship.get("recorded_commit") != commit:
            failures.append("qualification vector commit differs from provenance commit")

        consumption = data.get("qualification_consumption")
        profile = consumption.get("profile") if isinstance(consumption, dict) else None
        current_files = source_files_for_profile(
            data.get("qualification_content_vector"), profile
        )
        if data.get("source_files") != len(current_files):
            failures.append("source file count does not match current checkout")
        if source_hash != deterministic_source_hash(current_files):
            failures.append("source hash does not match current checkout")
        expected_manifest = {
            path.relative_to(ROOT_DIR).as_posix(): sha256_file(path) for path in current_files
        }
        if data.get("source_manifest") != expected_manifest:
            failures.append("source manifest does not match current checkout")
        expected_configs = default_configs_for_profile(
            data.get("qualification_content_vector"), profile
        )
        if tuple(sorted(config_hashes)) != expected_configs:
            failures.append(
                "config hashes do not exactly match the qualification profile defaults"
            )
    except Exception as exc:
        failures.append(f"could not recompute source provenance: {exc}")
    if data.get("acceptance_eligible") is not True:
        failures.append("provenance generator marked the run ineligible")
    if failures:
        return gate("failed", "provenance is recorded but not acceptance-eligible", {"failures": failures})
    return gate("passed", "source, config, dependency, and container provenance is recorded")


def joint_runtime_status(run_dir: Path) -> dict[str, Any]:
    path, data = _structured_file(run_dir, "metrics/joint_runtime.json")
    if not data:
        return gate("not_run", f"{path.relative_to(run_dir)} is missing or invalid")
    overlap = data.get("required_overlap_s")
    components = data.get("components") if isinstance(data.get("components"), list) else []
    failures: list[str] = []
    if data.get("schema_version") != 2:
        failures.append("joint runtime schema_version is not 2")
    if data.get("run_id") != run_dir.name:
        failures.append("joint runtime run_id does not match")
    runtime_id = data.get("runtime_id")
    if not isinstance(runtime_id, str) or len(runtime_id) < 8:
        failures.append("joint runtime runtime_id is missing")
    provenance = load_json(run_dir / "metrics/provenance.json")
    if data.get("source_hash") != provenance.get("source_hash"):
        failures.append("joint runtime source_hash does not match provenance")
    if not finite_number(overlap) or float(overlap) < 300:
        failures.append("required component overlap is below 300 seconds")
    required_names = {"gazebo", "ardupilot", "position_tracker", "bridge", "ns3", "sionna", "traffic_endpoints"}
    observed_names = {str(item.get("name")) for item in components if isinstance(item, dict)}
    if not required_names.issubset(observed_names):
        failures.append("required components are missing: " + ", ".join(sorted(required_names - observed_names)))
    for item in components:
        if not isinstance(item, dict):
            continue
        if item.get("ready") is not True or item.get("healthy") is not True:
            failures.append(f"component {item.get('name')} is not ready and healthy")
        if item.get("exit_code") not in (None, 0):
            failures.append(f"component {item.get('name')} exit_code={item.get('exit_code')}")
    if data.get("errors"):
        failures.append("joint runtime reports errors")
    if data.get("raw_event_log") != JOINT_RUNTIME_EVENT_LOG:
        failures.append(
            f"joint runtime raw_event_log must be {JOINT_RUNTIME_EVENT_LOG}"
        )
    raw_path, raw_error = checked_run_file(run_dir, JOINT_RUNTIME_EVENT_LOG)
    events: list[dict[str, Any]] = []
    if raw_error:
        failures.append(raw_error)
    elif raw_path is not None:
        if data.get("raw_event_sha256") != sha256_file(raw_path):
            failures.append("joint runtime raw-event hash mismatch")
        events, raw_failures = load_jsonl(raw_path)
        failures.extend(raw_failures)
        failures.extend(
            raw_event_envelope_failures(
                events,
                run_id=run_dir.name,
                runtime_id=runtime_id,
                source_hash=data.get("source_hash"),
            )
        )
    allowed_events = {
        "joint_runtime_start",
        "component_sample",
        "component_exit",
        "joint_runtime_complete",
    }
    unexpected_events = sorted(
        {
            str(event.get("event"))
            for event in events
            if event.get("event") not in allowed_events
        }
    )
    if unexpected_events:
        failures.append(
            "joint runtime raw log contains unexpected event types: "
            + ", ".join(unexpected_events)
        )
    start_events = [event for event in events if event.get("event") == "joint_runtime_start"]
    completion_events = [
        event for event in events if event.get("event") == "joint_runtime_complete"
    ]
    if len(start_events) != 1 or len(completion_events) != 1:
        failures.append("joint runtime raw log needs exactly one start and completion event")
    else:
        start = start_events[0]
        completion = completion_events[0]
        if not events or events[0] is not start or events[-1] is not completion:
            failures.append("joint runtime start/completion do not bound the raw log")
        if completion.get("errors") not in (None, []):
            failures.append("joint runtime completion reports errors")
        if (
            start.get("run_id") != run_dir.name
            or start.get("runtime_id") != runtime_id
            or start.get("source_hash") != data.get("source_hash")
        ):
            failures.append("joint runtime raw start identity does not match")
    raw_ranges: dict[str, tuple[float, float]] = {}
    for name in required_names:
        samples = [
            event
            for event in events
            if event.get("event") == "component_sample" and event.get("component") == name
        ]
        timestamps = [sample.get("monotonic_ns") for sample in samples]
        metrics = sequence_metrics_ns(timestamps)
        if metrics is None or len(samples) < 2:
            failures.append(f"component {name} lacks advancing raw health samples")
            continue
        if any(
            sample.get("ready") is not True
            or sample.get("healthy") is not True
            or not nonnegative_integer(sample.get("pid"))
            or sample.get("pid") <= 0
            for sample in samples
        ):
            failures.append(f"component {name} has unhealthy raw samples")
        if metrics["max_gap_s"] > 5.0:
            failures.append(f"component {name} health-sample gap exceeds 5 seconds")
        raw_ranges[name] = (metrics["first_ns"], metrics["last_ns"])
    if len(raw_ranges) == len(required_names):
        raw_overlap = (min(last for _, last in raw_ranges.values()) - max(first for first, _ in raw_ranges.values())) / 1_000_000_000
        if raw_overlap < 300:
            failures.append(f"raw component overlap is only {raw_overlap:.3f} seconds")
        if finite_number(overlap) and not math.isclose(
            float(overlap), raw_overlap, rel_tol=0.0, abs_tol=1.0
        ):
            failures.append("summarized and raw component overlap differ")
    if any(event.get("event") == "component_exit" for event in events):
        failures.append("joint runtime raw log contains component exit")
    return gate(
        "passed" if not failures else "failed",
        "joint runtime raw component overlap evaluated",
        {"failures": failures},
    )


def _m1_decode_cmdline(raw: bytes) -> list[str]:
    if not raw:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.rstrip(b"\0").split(b"\0")]


_PROTO_TEXT_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|[A-Za-z_][A-Za-z0-9_.]*"
    r"|[+-]?(?:0[xX][0-9A-Fa-f]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"|[{}:\[\],;<>]"
)


def _protobuf_text_tokens(text: str) -> list[str]:
    """Tokenize protobuf text without treating comments as message content."""

    tokens: list[str] = []
    offset = 0
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        if text.startswith("//", offset) or text[offset] == "#":
            newline = text.find("\n", offset)
            offset = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", offset):
            closing = text.find("*/", offset + 2)
            if closing < 0:
                raise ValueError("Gazebo Scene protobuf text has an unterminated comment")
            offset = closing + 2
            continue
        match = _PROTO_TEXT_TOKEN.match(text, offset)
        if match is None:
            excerpt = text[offset : offset + 16].splitlines()[0]
            raise ValueError(
                f"Gazebo Scene protobuf text has invalid syntax near {excerpt!r}"
            )
        tokens.append(match.group(0))
        offset = match.end()
    return tokens


def gazebo_top_level_model_names(text: str) -> list[str]:
    """Parse direct ``Scene.model`` names from Gazebo protobuf text output.

    A flat search for ``name:`` is unsafe because links, visuals and sensors use
    the same field spelling.  This small protobuf-text scanner intentionally
    recognizes only model blocks at brace depth zero and only their direct name
    field.  It raises on malformed or ambiguous input so callers fail closed.
    """

    if not isinstance(text, str) or not text.strip():
        raise ValueError("Gazebo Scene protobuf text is empty")
    tokens = _protobuf_text_tokens(text)
    names: list[str] = []
    depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if depth == 0 and token == "model":
            if index + 1 >= len(tokens) or tokens[index + 1] != "{":
                raise ValueError("top-level Scene.model is not a message block")
            index += 2
            block_depth = 1
            model_name: str | None = None
            while index < len(tokens) and block_depth:
                token = tokens[index]
                if token == "{":
                    block_depth += 1
                    index += 1
                    continue
                if token == "}":
                    block_depth -= 1
                    index += 1
                    continue
                if (
                    block_depth == 1
                    and token == "name"
                    and index + 2 < len(tokens)
                    and tokens[index + 1] == ":"
                    and tokens[index + 2].startswith('"')
                ):
                    if model_name is not None:
                        raise ValueError("top-level Scene.model has duplicate name fields")
                    try:
                        decoded_name = json.loads(tokens[index + 2])
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Scene.model name is not a valid quoted string: {exc}") from exc
                    if not isinstance(decoded_name, str) or not decoded_name:
                        raise ValueError("top-level Scene.model name is empty")
                    model_name = decoded_name
                    index += 3
                    continue
                index += 1
            if block_depth != 0:
                raise ValueError("top-level Scene.model block is unterminated")
            if model_name is None:
                raise ValueError("top-level Scene.model has no direct name field")
            names.append(model_name)
            continue
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Gazebo Scene protobuf text has an unmatched closing brace")
        index += 1
    if depth != 0:
        raise ValueError("Gazebo Scene protobuf text has unterminated message blocks")
    return names


def _m1_locked_runtime_identity(
    provenance: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Load the exact-image M1 process manifest bound by run provenance."""

    failures: list[str] = []
    lock_path = ROOT_DIR / M1_DEPENDENCY_LOCK_PATH
    try:
        loaded = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return {}, [f"M1 runtime identity lock is unavailable: {exc}"]
    lock = loaded if isinstance(loaded, dict) else {}
    identity = lock.get("m1_runtime_identity")
    if not isinstance(identity, dict) or identity.get("schema_version") != 1:
        return {}, ["dependency lock lacks m1_runtime_identity schema 1"]

    config_hashes = (
        provenance.get("config_hashes")
        if isinstance(provenance.get("config_hashes"), dict)
        else {}
    )
    if config_hashes.get(M1_DEPENDENCY_LOCK_PATH) != sha256_file(lock_path):
        failures.append("run provenance does not bind the current M1 runtime identity lock")
    container = (
        provenance.get("container_image")
        if isinstance(provenance.get("container_image"), dict)
        else {}
    )
    if identity.get("container_image_digest") != container.get("digest"):
        failures.append("M1 runtime identity lock does not match the accepted run image")

    executable_sha256 = identity.get("executable_sha256")
    role_paths = identity.get("role_executable_path")
    invoked_sha256 = identity.get("invoked_file_sha256")
    role_invoked_paths = identity.get("role_invoked_file_path")
    for label, mapping in (
        ("executable_sha256", executable_sha256),
        ("role_executable_path", role_paths),
        ("invoked_file_sha256", invoked_sha256),
        ("role_invoked_file_path", role_invoked_paths),
    ):
        if not isinstance(mapping, dict) or not mapping:
            failures.append(f"M1 runtime identity {label} is missing")
    if failures:
        return identity, failures

    for path, digest in {**executable_sha256, **invoked_sha256}.items():
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or str(Path(path)) != path
            or re.fullmatch(r"[0-9a-f]{64}", str(digest or "")) is None
        ):
            failures.append(f"M1 runtime identity has invalid canonical path/hash: {path!r}")
    if set(role_paths) != set(M1_REQUIRED_PROCESS_COUNTS):
        failures.append("M1 role executable-path manifest is not exact")
    for role, path in role_paths.items():
        if path not in executable_sha256:
            failures.append(f"M1 role {role} names an unlocked executable path")
    if set(role_invoked_paths) != {"mavproxy"}:
        failures.append("M1 role invoked-file manifest is not exact")
    elif role_invoked_paths.get("mavproxy") not in invoked_sha256:
        failures.append("M1 MAVProxy invoked file is not hash locked")
    return identity, failures


def _m1_mavproxy_invoked_path(argv: list[str]) -> str | None:
    names = [Path(value).name.lower() for value in argv]
    if names and names[0] == "mavproxy.py":
        return argv[0]
    if len(names) >= 2 and re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", names[0]):
        if names[1] == "mavproxy.py":
            return argv[1]
    return None


def _m1_locked_process_identity_failures(
    process: dict[str, Any],
    argv: list[str],
    role: str | None,
    identity: dict[str, Any],
    context: str,
) -> list[str]:
    """Compare one raw /proc identity to the exact accepted-image manifest."""

    failures: list[str] = []
    executable_sha256 = (
        identity.get("executable_sha256")
        if isinstance(identity.get("executable_sha256"), dict)
        else {}
    )
    role_paths = (
        identity.get("role_executable_path")
        if isinstance(identity.get("role_executable_path"), dict)
        else {}
    )
    exe_path = process.get("exe_path")
    expected_hash = executable_sha256.get(exe_path)
    if expected_hash is None:
        failures.append(f"{context}.exe_path is absent from the accepted-image manifest")
    elif process.get("exe_sha256") != expected_hash:
        failures.append(f"{context}.exe_sha256 differs from the accepted executable bytes")
    if role in role_paths and exe_path != role_paths[role]:
        failures.append(f"{context} uses the wrong canonical executable for role {role}")

    invoked_files = process.get("invoked_files")
    if not isinstance(invoked_files, dict):
        failures.append(f"{context}.invoked_files is missing")
        invoked_files = {}
    if role == "mavproxy":
        role_invoked_paths = (
            identity.get("role_invoked_file_path")
            if isinstance(identity.get("role_invoked_file_path"), dict)
            else {}
        )
        invoked_sha256 = (
            identity.get("invoked_file_sha256")
            if isinstance(identity.get("invoked_file_sha256"), dict)
            else {}
        )
        expected_path = role_invoked_paths.get("mavproxy")
        actual_path = _m1_mavproxy_invoked_path(argv)
        if actual_path != expected_path:
            failures.append(f"{context} MAVProxy script path is not canonical")
        expected_invoked = (
            {expected_path: invoked_sha256.get(expected_path)}
            if isinstance(expected_path, str)
            else {}
        )
        if invoked_files != expected_invoked:
            failures.append(f"{context} MAVProxy script bytes differ from the accepted manifest")
    elif invoked_files != {}:
        failures.append(f"{context} reports an undeclared invoked executable file")
    return failures


def _m1_process_role(process: dict[str, Any], argv: list[str]) -> str | None:
    exe_name = Path(str(process.get("exe_path") or "")).name.lower()
    argv_names = [Path(value).name.lower() for value in argv]
    argv0 = argv_names[0] if argv_names else ""
    if exe_name == "arducopter" and argv0 == "arducopter":
        return "arducopter"
    python_executable = re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", exe_name) is not None
    mavproxy_script = (
        argv0 == "mavproxy.py"
        or (
            re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", argv0) is not None
            and len(argv_names) >= 2
            and argv_names[1] == "mavproxy.py"
        )
    )
    if python_executable and mavproxy_script:
        return "mavproxy"
    if exe_name == "micro_ros_agent" and argv0 == "micro_ros_agent":
        return "micro_ros_agent"
    launcher_is_server = exe_name.startswith("ruby")
    has_gz_sim = any(
        argv_names[index] == "gz" and argv[index + 1].lower() == "sim"
        for index in range(max(0, len(argv) - 1))
    )
    try:
        process_title = shlex.split(argv[0]) if len(argv) == 1 else []
    except ValueError:
        process_title = []
    has_gz_sim_title = (
        len(process_title) >= 2
        and Path(process_title[0]).name.lower() == "gz"
        and process_title[1].lower() == "sim"
    )
    if launcher_is_server and (
        (has_gz_sim and "-s" in argv)
        or (has_gz_sim_title and "-s" in process_title)
    ):
        return "gazebo_server"
    return None


def _m1_process_identity_digest(process: dict[str, Any]) -> str:
    payload = {
        key: process.get(key)
        for key in (
            "pid",
            "ppid",
            "pgid",
            "session_id",
            "start_ticks",
            "cmdline_sha256",
            "exe_path",
            "exe_sha256",
            "uid",
            "namespaces",
            "capabilities",
            "invoked_files",
        )
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _m1_identity_set_sha256(identity_digests: Iterable[str]) -> str:
    canonical = json.dumps(sorted(identity_digests), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _m1_argv_option(argv: list[str], name: str) -> str | None:
    values: list[str] = []
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            values.append(argv[index + 1])
        if value.startswith(f"{name}="):
            values.append(value.split("=", 1)[1])
    if len(values) > 1:
        raise ValueError(f"duplicate option {name}")
    return values[0] if values else None


def _m1_runtime_endpoints(
    processes: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Independently derive the active SITL/DDS endpoint matrix from raw argv."""
    failures: list[str] = []
    endpoints: dict[str, list[dict[str, Any]]] = {
        role: [] for role in M1_REQUIRED_PROCESS_COUNTS
    }
    for process in processes:
        if not isinstance(process, dict):
            continue
        try:
            raw_cmdline = base64.b64decode(
                str(process.get("cmdline_b64") or ""), validate=True
            )
        except (ValueError, TypeError):
            raw_cmdline = b""
        argv = _m1_decode_cmdline(raw_cmdline)
        role = _m1_process_role(process, argv)
        pid = process.get("pid")
        try:
            if role == "arducopter":
                instance = int(str(_m1_argv_option(argv, "--instance")))
                endpoints[role].append(
                    {
                        "pid": pid,
                        "instance": instance,
                        "system_id": int(str(_m1_argv_option(argv, "--sysid"))),
                        "sim_address": _m1_argv_option(argv, "--sim-address"),
                        "fdm_port_in": 9002 + 10 * instance,
                    }
                )
            elif role == "mavproxy":
                endpoints[role].append(
                    {
                        "pid": pid,
                        "master": _m1_argv_option(argv, "--master"),
                        "sitl": _m1_argv_option(argv, "--sitl"),
                        "out": _m1_argv_option(argv, "--out"),
                        "default_modules": _m1_argv_option(argv, "--default-modules"),
                    }
                )
            elif role == "micro_ros_agent":
                namespace = next(
                    (
                        value.split(":=", 1)[1]
                        for value in argv
                        if value.startswith("__ns:=")
                    ),
                    None,
                )
                endpoints[role].append(
                    {
                        "pid": pid,
                        "transport": argv[1] if len(argv) > 1 else None,
                        "port": int(str(_m1_argv_option(argv, "--port"))),
                        "namespace": namespace,
                    }
                )
            elif role == "gazebo_server":
                endpoints[role].append({"pid": pid})
        except (TypeError, ValueError):
            failures.append(f"raw process {pid} has malformed {role} endpoint arguments")
    for role in endpoints:
        endpoints[role].sort(
            key=lambda item: (
                int(item.get("instance", item.get("port", item.get("pid", -1))) or -1),
                int(item.get("pid") or -1),
            )
        )

    arducopter = endpoints["arducopter"]
    if [item.get("instance") for item in arducopter] != list(range(5)):
        failures.append("raw ArduCopter instances are not exactly 0..4")
    if [item.get("system_id") for item in arducopter] != list(range(1, 6)):
        failures.append("raw ArduCopter system IDs are not exactly 1..5")
    if {item.get("sim_address") for item in arducopter} != {"127.0.0.1"}:
        failures.append("raw ArduCopter simulation address is not declared loopback")

    mavproxy = endpoints["mavproxy"]
    if {item.get("master") for item in mavproxy} != {
        f"tcp:127.0.0.1:{5760 + 10 * index}" for index in range(5)
    }:
        failures.append("raw MAVProxy master endpoints are not five unique SITL TCP endpoints")
    if {item.get("sitl") for item in mavproxy} != {
        f"127.0.0.1:{5501 + 10 * index}" for index in range(5)
    }:
        failures.append("raw MAVProxy SITL endpoints are not five unique simulator endpoints")
    if {(item.get("master"), item.get("sitl")) for item in mavproxy} != {
        (
            f"tcp:127.0.0.1:{5760 + 10 * index}",
            f"127.0.0.1:{5501 + 10 * index}",
        )
        for index in range(5)
    }:
        failures.append("raw MAVProxy master/SITL endpoint pairing does not match instances")
    if {item.get("out") for item in mavproxy} != {"127.0.0.1:14550"}:
        failures.append("raw MAVProxy health output is not the declared aggregator")
    if {item.get("default_modules") for item in mavproxy} != {
        M1_MAVPROXY_OFFLINE_DEFAULT_MODULES
    }:
        failures.append("raw MAVProxy processes do not use the declared offline module set")

    agents = endpoints["micro_ros_agent"]
    if [item.get("port") for item in agents] != list(range(2019, 2024)):
        failures.append("raw micro-ROS DDS ports are not exactly 2019..2023")
    if [item.get("namespace") for item in agents] != [
        f"/uav{index}" for index in range(1, 6)
    ]:
        failures.append("raw micro-ROS namespaces are not exactly /uav1../uav5")
    if {item.get("transport") for item in agents} != {"udp4"}:
        failures.append("raw micro-ROS agents do not use declared UDP transport")
    return endpoints, failures


def _m1_odometry_event_failures(record: dict[str, Any], context: str) -> list[str]:
    failures: list[str] = []
    vectors: dict[str, tuple[int, list[Any]]] = {}
    for key, size in (
        ("position_m", 3),
        ("orientation_xyzw", 4),
        ("linear_velocity_mps", 3),
        ("angular_velocity_rad_s", 3),
    ):
        value = record.get(key)
        if not isinstance(value, list) or len(value) != size or any(
            not finite_number(item) for item in value
        ):
            failures.append(f"{context}.{key} is not a finite {size}-vector")
        else:
            vectors[key] = (size, value)
    if "orientation_xyzw" in vectors:
        quaternion_norm = math.sqrt(
            sum(float(value) ** 2 for value in vectors["orientation_xyzw"][1])
        )
        if not 0.5 <= quaternion_norm <= 1.5:
            failures.append(f"{context} quaternion norm is outside [0.5, 1.5]")
    if "linear_velocity_mps" in vectors:
        derived_speed = math.sqrt(
            sum(float(value) ** 2 for value in vectors["linear_velocity_mps"][1])
        )
        if not finite_number(record.get("linear_speed_mps")) or not math.isclose(
            float(record["linear_speed_mps"]), derived_speed, rel_tol=0.0, abs_tol=1e-9
        ):
            failures.append(f"{context}.linear_speed_mps differs from raw velocity")
    if failures and record.get("valid") is True:
        failures.append(f"{context} producer valid flag contradicts raw fields")
    elif not failures and record.get("valid") is not True:
        failures.append(f"{context} producer valid flag is false despite valid raw fields")
    return failures


def _m1_xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _m1_parse_robot_description(
    raw: bytes, context: str
) -> tuple[dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    try:
        root = ET.fromstring(raw.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        return None, [f"{context} is not exact UTF-8 XML: {exc}"]
    plugins = [
        element
        for element in root.iter()
        if _m1_xml_local_name(element.tag) == "plugin"
        and (
            str(element.attrib.get("name", "")) == "ArduPilotPlugin"
            or str(element.attrib.get("filename", "")) == "ArduPilotPlugin"
        )
    ]
    if len(plugins) != 1:
        return None, [f"{context} has {len(plugins)} ArduPilotPlugin elements"]
    children = {
        _m1_xml_local_name(child.tag): (child.text or "").strip()
        for child in plugins[0]
    }
    try:
        result = {
            "fdm_addr": children["fdm_addr"],
            "fdm_port_in": int(children["fdm_port_in"]),
        }
    except (KeyError, ValueError) as exc:
        failures.append(f"{context} has malformed FDM endpoint: {exc}")
        result = None
    return result, failures


def _m1_readiness_process_snapshot(
    sample: dict[str, Any], process_group: int, runtime_identity: dict[str, Any]
) -> tuple[dict[str, int], dict[str, set[str]], set[str], list[str]]:
    """Independently verify one raw readiness process snapshot."""
    failures: list[str] = []
    processes = sample.get("processes")
    counts = {role: 0 for role in M1_REQUIRED_PROCESS_COUNTS}
    role_digests = {role: set() for role in M1_REQUIRED_PROCESS_COUNTS}
    all_digests: set[str] = set()
    if not isinstance(processes, list) or not processes:
        return counts, role_digests, all_digests, ["readiness process snapshot is empty"]
    pids = [process.get("pid") for process in processes if isinstance(process, dict)]
    if len(pids) != len(processes) or len(set(pids)) != len(pids):
        failures.append("readiness process snapshot has duplicate/invalid PIDs")
    for index, process in enumerate(processes):
        context = f"readiness process[{index}]"
        if not isinstance(process, dict):
            failures.append(f"{context} is not an object")
            continue
        try:
            raw_cmdline = base64.b64decode(
                str(process.get("cmdline_b64") or ""), validate=True
            )
        except (ValueError, TypeError):
            raw_cmdline = b""
            failures.append(f"{context}.cmdline_b64 is invalid")
        argv = _m1_decode_cmdline(raw_cmdline)
        if hashlib.sha256(raw_cmdline).hexdigest() != process.get("cmdline_sha256"):
            failures.append(f"{context}.cmdline_sha256 differs from raw argv")
        if process.get("cmdline") != argv:
            failures.append(f"{context}.cmdline differs from raw argv")
        role = _m1_process_role(process, argv)
        if process.get("role") != role:
            failures.append(f"{context}.role differs from actual executable+argv")
        failures.extend(
            _m1_locked_process_identity_failures(
                process, argv, role, runtime_identity, context
            )
        )
        if role in counts:
            counts[role] += 1
        if process.get("state") not in M1_ALLOWED_PROCESS_STATES:
            failures.append(f"{context} has unhealthy state {process.get('state')!r}")
        if process.get("identity_errors") != []:
            failures.append(f"{context} has incomplete identity")
        if process.get("pgid") != process_group or process.get("session_id") != process_group:
            failures.append(f"{context} escaped the supervised group/session")
        for key in ("cmdline_sha256", "exe_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", str(process.get(key) or "")) is None:
                failures.append(f"{context}.{key} is not SHA-256")
        if not isinstance(process.get("exe_path"), str) or not str(
            process.get("exe_path")
        ).startswith("/"):
            failures.append(f"{context}.exe_path is invalid")
        namespaces = process.get("namespaces")
        if not isinstance(namespaces, dict) or set(namespaces) != set(M1_PROCESS_NAMESPACES):
            failures.append(f"{context}.namespaces is incomplete")
        capabilities = process.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != set(M1_CAPABILITIES):
            failures.append(f"{context}.capabilities is incomplete")
        digest = _m1_process_identity_digest(process)
        all_digests.add(digest)
        if role in role_digests:
            role_digests[role].add(digest)
    if sample.get("counts") != counts:
        failures.append("readiness producer counts differ from raw identities")
    for role, required in M1_REQUIRED_PROCESS_COUNTS.items():
        if counts[role] != required:
            failures.append(
                f"readiness snapshot has {counts[role]} {role}; expected exactly {required}"
            )
    _, endpoint_failures = _m1_runtime_endpoints(processes)
    failures.extend(endpoint_failures)
    return counts, role_digests, all_digests, failures


def five_uav_health_status(run_dir: Path) -> dict[str, Any]:
    path, data = _structured_file(run_dir, "metrics/five_uav_health.json")
    if not data:
        return gate("not_run", f"{path.relative_to(run_dir)} is missing or invalid")
    uavs = data.get("uavs") if isinstance(data.get("uavs"), list) else []
    failures = _BoundedFailureList()
    if data.get("schema_version") != 2:
        failures.append("five-UAV health schema_version must be 2")
    if str(data.get("run_id")) != run_dir.name:
        failures.append("five-UAV health run_id does not match run directory")
    runtime_id = data.get("runtime_id")
    if not isinstance(runtime_id, str) or len(runtime_id) < 8:
        failures.append("five-UAV health runtime_id is missing")
    provenance_path = run_dir / "metrics/provenance.json"
    provenance = load_json(provenance_path)
    provenance_sha256 = sha256_file(provenance_path)
    runtime_identity, runtime_identity_failures = _m1_locked_runtime_identity(provenance)
    failures.extend(runtime_identity_failures)
    if data.get("source_hash") != provenance.get("source_hash"):
        failures.append("five-UAV source_hash does not match provenance")
    current_contract_sha256 = sha256_file(ROOT_DIR / M1_PLAN_PATH)
    config_hashes = (
        provenance.get("config_hashes")
        if isinstance(provenance.get("config_hashes"), dict)
        else {}
    )
    if (
        data.get("contract") != M1_CONTRACT_ID
        or data.get("plan_version") != 3
        or data.get("contract_sha256") != current_contract_sha256
        or config_hashes.get(M1_PLAN_PATH) != current_contract_sha256
    ):
        failures.append("five-UAV health evidence is not bound to the current v3 contract")
    if data.get("provenance_sha256") != provenance_sha256:
        failures.append("five-UAV health evidence does not hash-bind current provenance")
    if data.get("profile") != M1_PROFILE:
        failures.append("five-UAV health profile is not m1_component")
    provenance_consumption = (
        provenance.get("qualification_consumption")
        if isinstance(provenance.get("qualification_consumption"), dict)
        else {}
    )
    provenance_consumed_hashes = provenance_consumption.get(
        "consumed_node_sha256"
    )
    if (
        provenance_consumption.get("profile") != M1_PROFILE
        or provenance_consumption.get("consumed_nodes") != ["Q0", "Q1"]
        or not isinstance(provenance_consumed_hashes, dict)
        or set(provenance_consumed_hashes) != {"Q0", "Q1"}
    ):
        failures.append("M1 provenance does not consume exactly Q0 and Q1")
    if data.get("scenario_id") != M1_SCENARIO_ID:
        failures.append("five-UAV health scenario_id is not scenario_5uav")
    if data.get("phases") != list(M1_PHASES):
        failures.append("five-UAV health phase manifest is invalid")
    if data.get("clock_domains") != {
        "monotonic": "CLOCK_MONOTONIC",
        "wall": "CLOCK_REALTIME",
        "correlation_event": "clock_correlation",
    }:
        failures.append("five-UAV clock-domain declaration is invalid")
    if data.get("component_only") is not True or data.get("packet_path_eligible") is not False:
        failures.append("five-UAV health evidence is not labeled component-only")
    observed_duration = data.get("observed_duration_s")
    minimum_duration = data.get("minimum_duration_s")
    if not finite_number(observed_duration) or float(observed_duration) < 300:
        failures.append("five-UAV observation duration is below 300 seconds")
    if not finite_number(minimum_duration) or float(minimum_duration) < 300:
        failures.append("five-UAV configured minimum duration is below 300 seconds")

    expected_launch_log = FIVE_UAV_LAUNCH_LOG
    launch_log = run_dir / expected_launch_log
    if data.get("launch_log") != expected_launch_log:
        failures.append("five-UAV launch_log is not the fixed run-relative path")
    observation_offset = data.get("launch_log_observation_offset")
    observation_end_offset = data.get("launch_log_observation_end_offset")
    observation_sha256 = data.get("launch_log_observation_sha256")
    if not launch_log.is_file():
        failures.append("five-UAV launch log is missing")
    elif (
        not nonnegative_integer(observation_offset)
        or not nonnegative_integer(observation_end_offset)
        or observation_offset > observation_end_offset
        or observation_end_offset > launch_log.stat().st_size
    ):
        failures.append("five-UAV bounded launch-log observation offsets are invalid")
    else:
        launch_bytes = launch_log.read_bytes()[:observation_end_offset]
        if hashlib.sha256(launch_bytes).hexdigest() != observation_sha256:
            failures.append("five-UAV bounded launch-log prefix hash differs from summary")
        full_launch_text = launch_bytes.decode(errors="replace").lower()
        observation_text = launch_bytes[observation_offset:observation_end_offset].decode(errors="replace").lower()
        for marker in (
            "bind error",
            "bind failed",
            "failed to bind",
            "address already in use",
            "segmentation fault",
            "core dumped",
            "process has died",
            "error while starting ipvx agent",
            "failed to open (",
            "traceback (most recent call last)",
            "failed to download /srtm",
            "failed to download /srtm3",
        ):
            if marker in full_launch_text:
                failures.append(f"five-UAV launch log contains fatal marker {marker!r}")
        for marker in ("link 1 down", "no link"):
            if marker in observation_text:
                failures.append(
                    f"five-UAV launch log contains runtime link failure {marker!r}"
                )
        if re.search(r"--serial2[^\n]*ttyros", full_launch_text):
            failures.append("five-UAV launch configured an undeclared ttyROS SERIAL2 endpoint")

    if data.get("raw_event_log") != FIVE_UAV_HEALTH_EVENT_LOG:
        failures.append(
            f"five-UAV raw_event_log must be {FIVE_UAV_HEALTH_EVENT_LOG}"
        )
    raw_path, raw_path_error = checked_run_file(run_dir, FIVE_UAV_HEALTH_EVENT_LOG)
    records: list[dict[str, Any]] = []
    if raw_path_error:
        failures.append(raw_path_error)
    elif raw_path is not None:
        expected_hash = data.get("raw_event_sha256")
        actual_hash = sha256_file(raw_path)
        if not isinstance(expected_hash, str) or expected_hash != actual_hash:
            failures.append("five-UAV raw event hash does not match")
        records, jsonl_failures = load_jsonl(raw_path)
        failures.extend(jsonl_failures)
        failures.extend(
            raw_event_envelope_failures(
                records,
                run_id=run_dir.name,
                runtime_id=runtime_id,
                source_hash=data.get("source_hash"),
            )
        )
        for index, record in enumerate(records):
            context = f"raw[{index}]"
            if (
                record.get("contract") != M1_CONTRACT_ID
                or record.get("plan_version") != 3
                or record.get("contract_sha256") != current_contract_sha256
            ):
                failures.append(f"{context} is not bound to the current v3 contract")
            if record.get("provenance_sha256") != provenance_sha256:
                failures.append(f"{context}.provenance_sha256 does not match")
            if record.get("profile") != M1_PROFILE:
                failures.append(f"{context}.profile does not match M1")
            if record.get("scenario_id") != M1_SCENARIO_ID:
                failures.append(f"{context}.scenario_id does not match M1")
            if record.get("phase") not in M1_PHASES:
                failures.append(f"{context}.phase is not declared")
            wall_time_ns = record.get("wall_time_ns")
            if not nonnegative_integer(wall_time_ns):
                failures.append(f"{context}.wall_time_ns is invalid")
            else:
                try:
                    parsed_wall = datetime.fromisoformat(
                        str(record.get("wall_utc")).replace("Z", "+00:00")
                    )
                    parsed_wall_ns = int(parsed_wall.timestamp() * 1_000_000_000)
                except (TypeError, ValueError, OverflowError):
                    parsed_wall_ns = -1
                if abs(int(wall_time_ns) - parsed_wall_ns) > 1_000_000_000:
                    failures.append(f"{context}.wall clocks do not correlate")
        phase_order = {phase: index for index, phase in enumerate(M1_PHASES)}
        ordered_phases = [
            phase_order[record["phase"]]
            for record in records
            if record.get("phase") in phase_order
        ]
        if any(current < previous for previous, current in zip(ordered_phases, ordered_phases[1:])):
            failures.append("raw M1 readiness/measurement/finalization phases are not ordered")

    starts = [record for record in records if record.get("event") == "health_probe_start"]
    measurement_starts = [record for record in records if record.get("event") == "measurement_start"]
    completions = [record for record in records if record.get("event") == "health_probe_complete"]
    clock_correlations = [
        record for record in records if record.get("event") == "clock_correlation"
    ]
    measurement_start_ns: int | None = None
    measurement_end_ns: int | None = None
    if len(starts) != 1 or len(measurement_starts) != 1 or len(completions) != 1:
        failures.append(
            "raw health log must have exactly one probe start, measurement start, and completion event"
        )
    else:
        start = starts[0]
        measurement_start = measurement_starts[0]
        completion = completions[0]
        if not records or records[0] is not start or records[-1] is not completion:
            failures.append("raw M1 start/completion do not bound the health log")
        if (
            start.get("phase") != "readiness"
            or measurement_start.get("phase") != "measurement"
            or completion.get("phase") != "finalization"
        ):
            failures.append("raw M1 lifecycle events use the wrong phase identities")
        if (
            start.get("component_only") is not True
            or start.get("packet_path_eligible") is not False
            or start.get("expected_uavs") != [f"uav{index}" for index in range(1, 6)]
            or not finite_number(start.get("duration_s"))
            or float(start.get("duration_s")) < 300.0
        ):
            failures.append("raw M1 start does not declare the five-UAV 300-second component scope")
        readiness_timeout = start.get("readiness_timeout_s")
        if (
            not finite_number(readiness_timeout)
            or not 1.0 <= float(readiness_timeout) <= 120.0
        ):
            failures.append("raw M1 readiness timeout is outside [1, 120] seconds")
        readiness_stability = start.get("readiness_stability_s")
        if (
            not finite_number(readiness_stability)
            or not 1.0 <= float(readiness_stability) <= 30.0
        ):
            failures.append("raw M1 readiness stability dwell is outside [1, 30] seconds")
        if start.get("readiness_freshness_limits_s") != {
            "odometry": data.get("maximum_freshness_age_s"),
            "heartbeat": 3.0,
            "valid_home_position": 3.0,
        }:
            failures.append("raw M1 readiness freshness limits are not declared consistently")
        if nonnegative_integer(measurement_start.get("measurement_started_monotonic_ns")):
            measurement_start_ns = measurement_start["measurement_started_monotonic_ns"]
        else:
            failures.append("raw measurement-start monotonic timestamp is invalid")
        if nonnegative_integer(completion.get("measurement_ended_monotonic_ns")):
            measurement_end_ns = completion["measurement_ended_monotonic_ns"]
        else:
            failures.append("raw completion measurement timestamp is invalid")
        for key, expected in (
            ("run_id", run_dir.name),
            ("runtime_id", runtime_id),
            ("source_hash", data.get("source_hash")),
        ):
            if start.get(key) != expected:
                failures.append(f"raw health start {key} does not match summary")
            if measurement_start.get(key) != expected:
                failures.append(f"raw measurement start {key} does not match summary")
        if measurement_start.get("launch_log_observation_offset") != observation_offset:
            failures.append("raw measurement start does not bind the launch-log observation offset")
        for key, expected in (
            ("launch_log_observation_offset", observation_offset),
            ("launch_log_observation_end_offset", observation_end_offset),
            ("launch_log_observation_sha256", observation_sha256),
        ):
            if completion.get(key) != expected:
                failures.append(f"raw completion {key} differs from summary")
        raw_duration = completion.get("observed_duration_s")
        if not finite_number(raw_duration) or not finite_number(observed_duration) or not math.isclose(
            float(raw_duration), float(observed_duration), rel_tol=0.0, abs_tol=0.01
        ):
            failures.append("raw and summarized observation durations do not match")
        if completion.get("passed") is not True or completion.get("errors"):
            failures.append("raw completion event reports failure")
        dependency_versions = (
            provenance.get("dependency_versions")
            if isinstance(provenance.get("dependency_versions"), dict)
            else {}
        )
        runtime_capabilities = (
            dependency_versions.get("runtime_capabilities")
            if isinstance(dependency_versions.get("runtime_capabilities"), dict)
            else {}
        )
        container_image = (
            provenance.get("container_image")
            if isinstance(provenance.get("container_image"), dict)
            else {}
        )
        expected_runtime_identity = {
            "git_commit": provenance.get("git_commit"),
            "container_id": container_image.get("runtime_container_id"),
            "container_image_digest": container_image.get("digest"),
            "host": runtime_capabilities.get("host"),
            "kernel": {
                "system": runtime_capabilities.get("system"),
                "machine": runtime_capabilities.get("machine"),
                "release": runtime_capabilities.get("kernel_release"),
                "version": runtime_capabilities.get("kernel_version"),
            },
            "gpu": runtime_capabilities.get("gpu"),
        }
        if start.get("runtime_identity") != expected_runtime_identity:
            failures.append("raw M1 runtime host/container/kernel/GPU identity does not match provenance")

    final_readiness_role_digests: dict[str, set[str]] | None = None
    final_readiness_all_digests: set[str] | None = None
    readiness_completed_ns: int | None = None
    readiness_events = [record for record in records if record.get("event") == "readiness"]
    readiness_summary = (
        data.get("readiness") if isinstance(data.get("readiness"), dict) else {}
    )
    readiness_process_health = (
        data.get("process_health") if isinstance(data.get("process_health"), dict) else {}
    )
    readiness_process_group = readiness_process_health.get("process_group")
    if len(readiness_events) != 1:
        failures.append("raw M1 needs exactly one readiness decision")
    else:
        readiness = readiness_events[0]
        readiness_keys = (
            "ready",
            "timeout_s",
            "stability_s",
            "stability_started_monotonic_ns",
            "stability_completed_monotonic_ns",
            "qualifying_process_samples",
            "readiness_process_samples",
            "robot_description_errors",
            "launch_log_readiness_end_offset",
            "odometry_counts",
            "heartbeat_counts",
            "mavlink_position_counts",
            "mavlink_valid_home_position_counts",
            "odometry_age_s",
            "heartbeat_age_s",
            "mavlink_valid_home_position_age_s",
            "freshness_limits_s",
        )
        if readiness.get("phase") != "readiness" or readiness.get("ready") is not True:
            failures.append("raw M1 readiness decision is not a successful readiness-phase event")
        if readiness.get("error") is not None:
            failures.append("raw M1 readiness decision reports an error")
        elapsed = readiness.get("elapsed_s")
        timeout_s = readiness.get("timeout_s")
        if (
            not finite_number(timeout_s)
            or not finite_number(elapsed)
            or not 0.0 <= float(elapsed) <= float(timeout_s) + 0.25
            or not 1.0 <= float(timeout_s) <= 120.0
        ):
            failures.append("raw M1 readiness timing is outside the declared timeout")
        if len(starts) == 1 and timeout_s != starts[0].get("readiness_timeout_s"):
            failures.append("raw M1 readiness timeout differs from probe start")
        stability_s = readiness.get("stability_s")
        stability_started_ns = readiness.get("stability_started_monotonic_ns")
        stability_completed_ns = readiness.get("stability_completed_monotonic_ns")
        if nonnegative_integer(stability_completed_ns):
            readiness_completed_ns = int(stability_completed_ns)
        if (
            len(starts) != 1
            or stability_s != starts[0].get("readiness_stability_s")
            or not finite_number(stability_s)
            or not nonnegative_integer(stability_started_ns)
            or not nonnegative_integer(stability_completed_ns)
            or stability_completed_ns < stability_started_ns
            or (stability_completed_ns - stability_started_ns) / 1_000_000_000
            < float(stability_s)
        ):
            failures.append("raw M1 readiness decision lacks the declared stable dwell")
        if readiness.get("robot_description_errors") != []:
            failures.append("raw M1 readiness reports robot-description errors")
        if readiness.get("launch_log_readiness_end_offset") != observation_offset:
            failures.append("readiness log boundary does not equal measurement start offset")
        for key in readiness_keys:
            if readiness_summary.get(key) != readiness.get(key):
                failures.append(f"M1 readiness summary {key} differs from raw decision")
        summarized_elapsed = readiness_summary.get("elapsed_s")
        if not finite_number(summarized_elapsed) or not finite_number(elapsed) or not math.isclose(
            float(summarized_elapsed), float(elapsed), rel_tol=0.0, abs_tol=0.001
        ):
            failures.append("M1 readiness elapsed summary differs from raw decision")
        expected_names = [f"uav{index}" for index in range(1, 6)]
        expected_ids = [str(index) for index in range(1, 6)]
        odometry_counts = readiness.get("odometry_counts")
        heartbeat_counts = readiness.get("heartbeat_counts")
        position_counts = readiness.get("mavlink_position_counts")
        valid_position_counts = readiness.get("mavlink_valid_home_position_counts")
        if (
            not isinstance(odometry_counts, dict)
            or sorted(odometry_counts) != expected_names
            or any(not nonnegative_integer(value) or value < 2 for value in odometry_counts.values())
        ):
            failures.append("raw M1 readiness lacks two odometry samples for every UAV")
        for label, values, minimum in (
            ("heartbeat", heartbeat_counts, 1),
            ("MAVLink position", position_counts, 2),
            ("valid MAVLink home position", valid_position_counts, 2),
        ):
            if (
                not isinstance(values, dict)
                or sorted(values) != expected_ids
                or any(not nonnegative_integer(value) or value < minimum for value in values.values())
            ):
                failures.append(f"raw M1 readiness lacks required {label} samples")
        readiness_seq = readiness.get("event_seq")
        prior_readiness = [
            record
            for record in records
            if nonnegative_integer(readiness_seq)
            and record.get("event_seq", math.inf) < readiness_seq
            and record.get("phase") == "readiness"
        ]
        for name in expected_names:
            raw_odometry = [
                record
                for record in prior_readiness
                if record.get("event") == "readiness_odometry" and record.get("uav") == name
            ]
            raw_count = len(raw_odometry)
            if not isinstance(odometry_counts, dict) or raw_count < odometry_counts.get(name, math.inf):
                failures.append(f"{name}: raw readiness odometry does not support decision counts")
            if any(
                record.get("valid") is not True
                or record.get("source_topic") != f"/{name}/odometry"
                for record in raw_odometry
            ):
                failures.append(f"{name}: raw readiness odometry is invalid")
            for raw_index, record in enumerate(raw_odometry):
                failures.extend(
                    _m1_odometry_event_failures(
                        record, f"{name}.readiness_odometry[{raw_index}]"
                    )
                )
        for system_id in range(1, 6):
            key = str(system_id)
            raw_heartbeats = [
                record
                for record in prior_readiness
                if record.get("event") == "readiness_heartbeat"
                and record.get("system_id") == system_id
            ]
            raw_heartbeat_count = len(raw_heartbeats)
            raw_positions = [
                record
                for record in prior_readiness
                if record.get("event") == "readiness_mavlink_global_position"
                and record.get("system_id") == system_id
            ]
            raw_valid_positions = sum(
                record.get("component_id") == 1
                and finite_number(record.get("lat_deg"))
                and finite_number(record.get("lon_deg"))
                and finite_number(record.get("relative_alt_m"))
                and finite_number(record.get("home_distance_m"))
                and abs(float(record["lat_deg"])) >= 1.0
                and abs(float(record["lon_deg"])) >= 1.0
                and -20.0 <= float(record["relative_alt_m"]) <= 100.0
                and 0.0 <= float(record["home_distance_m"]) <= 500.0
                for record in raw_positions
            )
            if any(
                record.get("component_id") != 1
                or record.get("mav_type") != 2
                or record.get("autopilot") != 3
                for record in raw_heartbeats
            ):
                failures.append(f"system {system_id}: raw readiness heartbeat identity is invalid")
            if not isinstance(heartbeat_counts, dict) or raw_heartbeat_count < heartbeat_counts.get(key, math.inf):
                failures.append(f"system {system_id}: raw readiness heartbeat count is unsupported")
            if not isinstance(position_counts, dict) or len(raw_positions) < position_counts.get(key, math.inf):
                failures.append(f"system {system_id}: raw readiness position count is unsupported")
            if not isinstance(valid_position_counts, dict) or raw_valid_positions < valid_position_counts.get(key, math.inf):
                failures.append(f"system {system_id}: raw valid-home count is unsupported")

        freshness_limits = readiness.get("freshness_limits_s")
        expected_freshness_limits = {
            "odometry": data.get("maximum_freshness_age_s"),
            "heartbeat": 3.0,
            "valid_home_position": 3.0,
        }
        if freshness_limits != expected_freshness_limits:
            failures.append("raw M1 readiness freshness limits differ from contract")
        for label, ages, expected_keys, limit_key in (
            ("odometry", readiness.get("odometry_age_s"), expected_names, "odometry"),
            ("heartbeat", readiness.get("heartbeat_age_s"), expected_ids, "heartbeat"),
            (
                "valid-home position",
                readiness.get("mavlink_valid_home_position_age_s"),
                expected_ids,
                "valid_home_position",
            ),
        ):
            limit = expected_freshness_limits[limit_key]
            if (
                not isinstance(ages, dict)
                or sorted(ages) != sorted(expected_keys)
                or not finite_number(limit)
                or any(
                    not finite_number(value) or not 0.0 <= float(value) <= float(limit)
                    for value in ages.values()
                )
            ):
                failures.append(f"raw M1 readiness {label} is not currently fresh")

        readiness_process_events = [
            record
            for record in prior_readiness
            if record.get("event") == "readiness_process_sample"
        ]
        if readiness.get("readiness_process_samples") != len(readiness_process_events):
            failures.append("readiness process-sample count differs from raw events")
        stable_process_events = [
            record
            for record in readiness_process_events
            if nonnegative_integer(stability_started_ns)
            and nonnegative_integer(stability_completed_ns)
            and nonnegative_integer(record.get("sampled_monotonic_ns"))
            and stability_started_ns
            <= record["sampled_monotonic_ns"]
            <= stability_completed_ns
        ]
        if (
            readiness.get("qualifying_process_samples") != len(stable_process_events)
            or len(stable_process_events) < 2
        ):
            failures.append("stable readiness dwell lacks the declared raw process samples")
        stable_sample_ns = [
            record.get("sampled_monotonic_ns") for record in stable_process_events
        ]
        stable_timing = sequence_metrics_ns(stable_sample_ns)
        if (
            stable_timing is None
            or not nonnegative_integer(stability_started_ns)
            or not nonnegative_integer(stability_completed_ns)
            or stable_sample_ns[0] != stability_started_ns
            or stable_sample_ns[-1] != stability_completed_ns
            or stable_timing["max_gap_s"] > M1_PROCESS_SAMPLE_MAX_GAP_S
        ):
            failures.append(
                "stable readiness process samples exceed the "
                f"{M1_PROCESS_SAMPLE_MAX_GAP_S:g}-second continuity bound"
            )

        baseline_role_digests: dict[str, set[str]] | None = None
        baseline_all_digests: set[str] | None = None
        launch_bytes = launch_log.read_bytes() if launch_log.is_file() else b""
        for sample_index, sample in enumerate(stable_process_events):
            context = f"stable readiness sample[{sample_index}]"
            if (
                sample.get("phase") != "readiness"
                or sample.get("qualifying") is not True
                or sample.get("streams_ready") is not True
                or sample.get("process_healthy") is not True
                or sample.get("robot_descriptions_ready") is not True
                or sample.get("link_down_detected") is not False
                or sample.get("fatal_launch_marker_detected") is not False
                or sample.get("reasons") != []
                or sample.get("error") not in (None, "")
            ):
                failures.append(f"{context} is not independently eligible")
            scan_start = sample.get("launch_log_scanned_start_offset")
            scan_end = sample.get("launch_log_scanned_end_offset")
            if (
                not nonnegative_integer(scan_start)
                or not nonnegative_integer(scan_end)
                or scan_start > scan_end
                or scan_end > len(launch_bytes)
            ):
                failures.append(f"{context} has invalid launch-log scan bounds")
            else:
                scan_text = launch_bytes[scan_start:scan_end].decode(errors="replace").lower()
                if any(marker in scan_text for marker in ("link 1 down", "no link")):
                    failures.append(f"{context} launch-log bytes contain a link failure")
            _, role_digests, all_digests, sample_failures = _m1_readiness_process_snapshot(
                sample,
                int(readiness_process_group)
                if nonnegative_integer(readiness_process_group)
                else -1,
                runtime_identity,
            )
            failures.extend(f"{context}: {failure}" for failure in sample_failures)
            if baseline_role_digests is None:
                baseline_role_digests = role_digests
                baseline_all_digests = all_digests
            elif role_digests != baseline_role_digests or all_digests != baseline_all_digests:
                failures.append(f"{context} changed process identity during the dwell")

            if sample_index == len(stable_process_events) - 1:
                final_readiness_role_digests = {
                    role: set(digests) for role, digests in role_digests.items()
                }
                final_readiness_all_digests = set(all_digests)

            sample_ns = sample.get("sampled_monotonic_ns")
            details = sample.get("stream_details")
            if not isinstance(details, dict) or not nonnegative_integer(sample_ns):
                failures.append(f"{context} lacks raw stream freshness details")
                continue
            if (
                not nonnegative_integer(sample.get("monotonic_ns"))
                or abs(sample_ns - sample["monotonic_ns"]) > 1_000_000_000
            ):
                failures.append(f"{context} sampled clock is not bound to event monotonic time")
            for name in expected_names:
                raw_odometry = [
                    record
                    for record in prior_readiness
                    if record.get("event") == "readiness_odometry"
                    and record.get("uav") == name
                    and nonnegative_integer(record.get("monotonic_ns"))
                    and record["monotonic_ns"] <= sample_ns
                ]
                if len(raw_odometry) < 2:
                    failures.append(f"{context}: {name} lacks two current odometry samples")
                else:
                    failures.extend(
                        _m1_odometry_event_failures(raw_odometry[-1], f"{context}.{name}.odometry")
                    )
                    age_s = (sample_ns - raw_odometry[-1]["monotonic_ns"]) / 1_000_000_000
                    odometry_limit = expected_freshness_limits["odometry"]
                    if (
                        not finite_number(odometry_limit)
                        or not 0.0 <= age_s <= float(odometry_limit)
                    ):
                        failures.append(f"{context}: {name} odometry is stale")
            for system_id in range(1, 6):
                raw_heartbeats = [
                    record
                    for record in prior_readiness
                    if record.get("event") == "readiness_heartbeat"
                    and record.get("system_id") == system_id
                    and nonnegative_integer(record.get("monotonic_ns"))
                    and record["monotonic_ns"] <= sample_ns
                ]
                raw_positions = [
                    record
                    for record in prior_readiness
                    if record.get("event") == "readiness_mavlink_global_position"
                    and record.get("system_id") == system_id
                    and nonnegative_integer(record.get("monotonic_ns"))
                    and record["monotonic_ns"] <= sample_ns
                    and record.get("component_id") == 1
                    and finite_number(record.get("lat_deg"))
                    and finite_number(record.get("lon_deg"))
                    and abs(float(record["lat_deg"])) >= 1.0
                    and abs(float(record["lon_deg"])) >= 1.0
                ]
                if not raw_heartbeats or any(
                    raw_heartbeats[-1].get(key) != expected
                    for key, expected in (("component_id", 1), ("mav_type", 2), ("autopilot", 3))
                ):
                    failures.append(f"{context}: system {system_id} lacks current ArduPilot heartbeat")
                elif (sample_ns - raw_heartbeats[-1]["monotonic_ns"]) / 1_000_000_000 > 3.0:
                    failures.append(f"{context}: system {system_id} heartbeat is stale")
                if len(raw_positions) < 2:
                    failures.append(f"{context}: system {system_id} lacks two valid home positions")
                elif (sample_ns - raw_positions[-1]["monotonic_ns"]) / 1_000_000_000 > 3.0:
                    failures.append(f"{context}: system {system_id} valid home position is stale")

    robot_description_events = [
        record for record in records if record.get("event") == "robot_description_probe"
    ]
    robot_summary = (
        data.get("robot_descriptions")
        if isinstance(data.get("robot_descriptions"), list)
        else []
    )
    independently_parsed_robots: list[dict[str, Any]] = []
    if len(robot_description_events) != 1:
        failures.append("raw M1 needs exactly one live robot_description probe")
    else:
        probe = robot_description_events[0]
        probe_robots = probe.get("robots")
        if probe.get("phase") != "readiness" or not isinstance(probe_robots, list):
            failures.append("raw robot_description probe is outside readiness or malformed")
        else:
            for index, robot in enumerate(probe_robots):
                expected_name = f"uav{index + 1}"
                context = f"robot_description[{index}]"
                if not isinstance(robot, dict):
                    failures.append(f"{context} is not an object")
                    continue
                try:
                    raw_description = base64.b64decode(
                        str(robot.get("robot_description_b64") or ""), validate=True
                    )
                except (ValueError, TypeError):
                    raw_description = b""
                    failures.append(f"{context}.robot_description_b64 is invalid")
                actual_hash = hashlib.sha256(raw_description).hexdigest()
                if actual_hash != robot.get("robot_description_sha256"):
                    failures.append(f"{context} hash differs from exact parameter bytes")
                parsed, parse_failures = _m1_parse_robot_description(
                    raw_description, context
                )
                failures.extend(parse_failures)
                if (
                    robot.get("name") != expected_name
                    or robot.get("namespace") != f"/{expected_name}"
                ):
                    failures.append(f"{context} canonical UAV identity is invalid")
                if parsed is not None:
                    if (
                        parsed.get("fdm_addr") != "127.0.0.1"
                        or parsed.get("fdm_port_in") != 9002 + index * 10
                        or robot.get("fdm_addr") != parsed.get("fdm_addr")
                        or robot.get("fdm_port_in") != parsed.get("fdm_port_in")
                    ):
                        failures.append(f"{context} live ArduPilotPlugin FDM endpoint is invalid")
                    independently_parsed_robots.append(
                        {
                            "name": expected_name,
                            "namespace": f"/{expected_name}",
                            "fdm_addr": parsed.get("fdm_addr"),
                            "fdm_port_in": parsed.get("fdm_port_in"),
                            "robot_description_sha256": actual_hash,
                        }
                    )
    if independently_parsed_robots != robot_summary:
        failures.append("robot-description summary differs from independently parsed raw XML")

    gazebo_probe_events = [
        record for record in records if record.get("event") == "gazebo_scene_probe"
    ]
    if len(gazebo_probe_events) != 1:
        failures.append("raw M1 needs exactly one successful Gazebo scene probe")
    else:
        gazebo_probe = gazebo_probe_events[0]
        expected_gazebo_command = [
            "gz",
            "service",
            "-s",
            "/world/map/scene/info",
            "--reqtype",
            "gz.msgs.Empty",
            "--reptype",
            "gz.msgs.Scene",
            "--timeout",
            "5000",
            "--req",
            "",
        ]
        if (
            gazebo_probe.get("phase") != "finalization"
            or gazebo_probe.get("exit_code") != 0
            or gazebo_probe.get("world_name") != "map"
            or gazebo_probe.get("command") != expected_gazebo_command
        ):
            failures.append("raw Gazebo scene probe command/result is invalid")
        decoded_gazebo: dict[str, bytes] = {}
        for stream in ("stdout", "stderr"):
            try:
                raw_stream = base64.b64decode(
                    str(gazebo_probe.get(f"{stream}_b64") or ""), validate=True
                )
            except (ValueError, TypeError):
                raw_stream = b""
                failures.append(f"raw Gazebo {stream} base64 is invalid")
            if hashlib.sha256(raw_stream).hexdigest() != gazebo_probe.get(
                f"{stream}_sha256"
            ):
                failures.append(f"raw Gazebo {stream} hash differs from exact bytes")
            decoded_gazebo[stream] = raw_stream
        try:
            gazebo_text = decoded_gazebo.get("stdout", b"").decode(
                "utf-8", errors="strict"
            )
            top_level_models = gazebo_top_level_model_names(gazebo_text)
            raw_gazebo_names = sorted(
                name for name in top_level_models if name.startswith("uav")
            )
        except (UnicodeDecodeError, ValueError) as exc:
            failures.append(f"raw Gazebo Scene.model inventory is invalid: {exc}")
            raw_gazebo_names = []
        expected_gazebo_names = [f"uav{index}" for index in range(1, 6)]
        if (
            raw_gazebo_names != expected_gazebo_names
            or gazebo_probe.get("model_names") != raw_gazebo_names
            or data.get("gazebo_model_names") != raw_gazebo_names
            or data.get("gazebo_world_name") != "map"
        ):
            failures.append("Gazebo model inventory differs from exact raw scene response")

    correlations_by_point = {
        point: [
            record
            for record in clock_correlations
            if record.get("correlation_point") == point
        ]
        for point in ("measurement_start", "measurement_end")
    }
    if len(clock_correlations) != 2 or any(
        len(values) != 1 for values in correlations_by_point.values()
    ):
        failures.append("raw M1 needs exactly two measurement clock-correlation samples")
    else:
        start_clock = correlations_by_point["measurement_start"][0]
        end_clock = correlations_by_point["measurement_end"][0]
        for label, clock in (("start", start_clock), ("end", end_clock)):
            if (
                clock.get("phase") != "measurement"
                or clock.get("monotonic_clock") != "CLOCK_MONOTONIC"
                or clock.get("wall_clock") != "CLOCK_REALTIME"
                or not nonnegative_integer(clock.get("monotonic_ns"))
                or not nonnegative_integer(clock.get("wall_time_ns"))
            ):
                failures.append(f"raw M1 {label} clock correlation is malformed")
        start_clock_ns = start_clock.get("monotonic_ns")
        end_clock_ns = end_clock.get("monotonic_ns")
        if (
            measurement_start_ns is not None
            and nonnegative_integer(start_clock_ns)
            and start_clock_ns > measurement_start_ns
        ):
            failures.append("raw M1 start clock sample does not precede measurement")
        if (
            measurement_end_ns is not None
            and nonnegative_integer(end_clock_ns)
            and end_clock_ns < measurement_end_ns
        ):
            failures.append("raw M1 end clock sample does not follow measurement")
        if all(
            nonnegative_integer(clock.get(key))
            for clock in (start_clock, end_clock)
            for key in ("monotonic_ns", "wall_time_ns")
        ):
            start_offset = start_clock["wall_time_ns"] - start_clock["monotonic_ns"]
            end_offset = end_clock["wall_time_ns"] - end_clock["monotonic_ns"]
            if abs(end_offset - start_offset) > 1_000_000_000:
                failures.append("raw M1 CLOCK_REALTIME/CLOCK_MONOTONIC correlation drift exceeds 1 second")

    try:
        summary_started = datetime.fromisoformat(
            str(data.get("started_utc")).replace("Z", "+00:00")
        ).timestamp()
        summary_ended = datetime.fromisoformat(
            str(data.get("ended_utc")).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        failures.append("five-UAV UTC start/end timestamps are invalid")
    else:
        if summary_ended <= summary_started:
            failures.append("five-UAV UTC interval is not positive")
        if finite_number(observed_duration) and not math.isclose(
            summary_ended - summary_started,
            float(observed_duration),
            rel_tol=0.0,
            abs_tol=2.0,
        ):
            failures.append("five-UAV UTC interval differs from monotonic observation duration")
        raw_start_wall_ns = (
            measurement_starts[0].get("wall_time_ns") if len(measurement_starts) == 1 else None
        )
        end_clock_records = correlations_by_point.get("measurement_end", [])
        raw_end_wall_ns = (
            end_clock_records[0].get("wall_time_ns") if len(end_clock_records) == 1 else None
        )
        if (
            not nonnegative_integer(raw_start_wall_ns)
            or abs(summary_started - raw_start_wall_ns / 1_000_000_000) > 1.0
        ):
            failures.append("five-UAV UTC start does not correlate with raw measurement clock")
        if (
            not nonnegative_integer(raw_end_wall_ns)
            or abs(summary_ended - raw_end_wall_ns / 1_000_000_000) > 1.0
        ):
            failures.append("five-UAV UTC end does not correlate with raw measurement clock")

    names = {str(item.get("name")) for item in uavs if isinstance(item, dict)}
    expected = {f"uav{i}" for i in range(1, 6)}
    if names != expected:
        failures.append(f"UAV names are {sorted(names)}, expected {sorted(expected)}")
    sysids = []
    dds_ports = []
    for item in uavs:
        if not isinstance(item, dict):
            continue
        sysids.append(item.get("system_id"))
        dds_ports.append(item.get("dds_udp_port"))
        expected_name = str(item.get("name"))
        try:
            expected_index = int(expected_name.removeprefix("uav"))
        except ValueError:
            expected_index = -1
        if (
            item.get("system_id") != expected_index
            or item.get("dds_udp_port") != 2018 + expected_index
        ):
            failures.append(
                f"{expected_name}: system ID/DDS port do not match the canonical UAV identity"
            )
        for key in ("gazebo_model", "sitl_healthy", "heartbeat", "odometry_fresh"):
            if item.get(key) is not True:
                failures.append(f"{item.get('name')}: {key} is not true")
        heartbeat_rate = item.get("heartbeat_rate_hz")
        heartbeat_sim_age = item.get("heartbeat_sim_age_s")
        heartbeat_sim_start = item.get("heartbeat_sim_start_delay_s")
        heartbeat_sim_gap = item.get("heartbeat_sim_max_gap_s")
        heartbeat_wall_age = item.get("heartbeat_age_s")
        heartbeat_wall_gap = item.get("heartbeat_max_gap_s")
        odometry_rate = item.get("odometry_rate_hz")
        odometry_age = item.get("odometry_age_s")
        odometry_start = item.get("odometry_start_delay_s")
        odometry_gap = item.get("odometry_max_gap_s")
        realtime_factor = item.get("odometry_realtime_factor")
        if item.get("heartbeat_time_basis") != "odometry_sim_stamp":
            failures.append(f"{item.get('name')}: heartbeat time basis is not simulated time")
        if not finite_number(heartbeat_rate) or float(heartbeat_rate) < 0.8:
            failures.append(f"{item.get('name')}: heartbeat rate is below 0.8 Hz")
        for label, value in (
            ("simulated-time heartbeat age", heartbeat_sim_age),
            ("simulated-time heartbeat start", heartbeat_sim_start),
            ("simulated-time heartbeat gap", heartbeat_sim_gap),
        ):
            minimum = -0.1 if label == "simulated-time heartbeat start" else 0.0
            if not finite_number(value) or not minimum <= float(value) <= 3.0:
                failures.append(f"{item.get('name')}: {label} is outside [0, 3] seconds")
        for label, value in (("wall heartbeat age", heartbeat_wall_age), ("wall heartbeat gap", heartbeat_wall_gap)):
            if not finite_number(value) or not 0 <= float(value) <= 15.0:
                failures.append(f"{item.get('name')}: {label} is outside [0, 15] seconds")
        if not finite_number(odometry_rate) or float(odometry_rate) < 5.0:
            failures.append(f"{item.get('name')}: odometry rate is below 5 Hz")
        for label, value in (
            ("odometry age", odometry_age),
            ("odometry start", odometry_start),
            ("odometry gap", odometry_gap),
        ):
            if not finite_number(value) or not 0 <= float(value) <= 1.0:
                failures.append(f"{item.get('name')}: {label} is outside [0, 1] seconds")
        if not finite_number(realtime_factor) or not 0.1 <= float(realtime_factor) <= 1.1:
            failures.append(f"{item.get('name')}: odometry realtime factor is outside [0.1, 1.1]")
        if item.get("odometry_invalid_samples") != 0 or item.get("odometry_nonadvancing_stamps") != 0:
            failures.append(f"{item.get('name')}: odometry contains invalid or nonadvancing samples")
        displacement = item.get("odometry_max_displacement_m")
        speed = item.get("odometry_max_speed_mps")
        if not finite_number(displacement) or not 0 <= float(displacement) <= 20.0:
            failures.append(f"{item.get('name')}: odometry displacement exceeds 20 m")
        if not finite_number(speed) or not 0 <= float(speed) <= 100.0:
            failures.append(f"{item.get('name')}: odometry speed exceeds 100 m/s")
        if item.get("mavlink_pose") is not True or not nonnegative_integer(
            item.get("mavlink_position_count")
        ) or item.get("mavlink_position_count", 0) < 2:
            failures.append(f"{item.get('name')}: MAVLink pose evidence is missing")
        if not nonnegative_integer(item.get("mavlink_valid_home_position_count")) or item.get(
            "mavlink_valid_home_position_count", 0
        ) < 2:
            failures.append(f"{item.get('name')}: valid geodetic MAVLink pose is missing")

        if records:
            name = str(item.get("name"))
            system_id = item.get("system_id")
            odom_events = [
                record
                for record in records
                if record.get("event") == "odometry" and record.get("uav") == name
            ]
            heartbeat_events = [
                record
                for record in records
                if record.get("event") == "heartbeat" and record.get("system_id") == system_id
            ]
            pose_events = [
                record
                for record in records
                if record.get("event") == "mavlink_global_position"
                and record.get("system_id") == system_id
            ]
            if len(odom_events) != item.get("odometry_count"):
                failures.append(f"{name}: odometry raw count does not match summary")
            if len(heartbeat_events) != item.get("heartbeat_count"):
                failures.append(f"{name}: heartbeat raw count does not match summary")
            if len(pose_events) != item.get("mavlink_position_count"):
                failures.append(f"{name}: MAVLink pose raw count does not match summary")
            if any(record.get("valid") is not True for record in odom_events):
                failures.append(f"{name}: raw odometry contains invalid samples")
            for raw_index, record in enumerate(odom_events):
                failures.extend(
                    _m1_odometry_event_failures(
                        record, f"{name}.odometry[{raw_index}]"
                    )
                )
            if [record.get("sequence") for record in odom_events] != list(
                range(1, len(odom_events) + 1)
            ):
                failures.append(f"{name}: raw odometry sequence is not contiguous")
            if [record.get("sequence") for record in heartbeat_events] != list(
                range(1, len(heartbeat_events) + 1)
            ):
                failures.append(f"{name}: raw heartbeat sequence is not contiguous")
            if [record.get("sequence") for record in pose_events] != list(
                range(1, len(pose_events) + 1)
            ):
                failures.append(f"{name}: raw MAVLink pose sequence is not contiguous")
            if any(record.get("source_topic") != f"/{name}/odometry" for record in odom_events):
                failures.append(f"{name}: raw odometry source topic is not canonical")
            raw_positions: list[list[float]] = []
            raw_speeds: list[float] = []
            for record in odom_events:
                position = record.get("position_m")
                speed_value = record.get("linear_speed_mps")
                if (
                    not isinstance(position, list)
                    or len(position) != 3
                    or any(not finite_number(value) for value in position)
                    or not finite_number(speed_value)
                ):
                    failures.append(f"{name}: raw odometry position/speed is malformed")
                    continue
                raw_positions.append([float(value) for value in position])
                raw_speeds.append(float(speed_value))
            if raw_positions:
                first_position = raw_positions[0]
                last_position = raw_positions[-1]
                raw_max_displacement = max(
                    math.sqrt(
                        sum(
                            (position[index] - first_position[index]) ** 2
                            for index in range(3)
                        )
                    )
                    for position in raw_positions
                )
                raw_max_speed = max(raw_speeds)
                if item.get("odometry_first_position_m") != first_position:
                    failures.append(f"{name}: first odometry position differs from raw")
                if item.get("odometry_last_position_m") != last_position:
                    failures.append(f"{name}: last odometry position differs from raw")
                if not finite_number(item.get("odometry_max_displacement_m")) or not math.isclose(
                    float(item["odometry_max_displacement_m"]),
                    raw_max_displacement,
                    rel_tol=0.0,
                    abs_tol=1e-5,
                ):
                    failures.append(f"{name}: maximum displacement differs from raw odometry")
                if not finite_number(item.get("odometry_max_speed_mps")) or not math.isclose(
                    float(item["odometry_max_speed_mps"]),
                    raw_max_speed,
                    rel_tol=0.0,
                    abs_tol=1e-5,
                ):
                    failures.append(f"{name}: maximum speed differs from raw odometry")
            if any(
                record.get("component_id") != 1
                or record.get("mav_type") != 2
                or record.get("autopilot") != 3
                for record in heartbeat_events
            ):
                failures.append(f"{name}: raw heartbeat is not from the expected ArduPilot vehicle component")
            raw_pose_values: list[tuple[float, float]] = []
            raw_valid_home_count = 0
            for record in pose_events:
                lat = record.get("lat_deg")
                lon = record.get("lon_deg")
                relative_alt = record.get("relative_alt_m")
                home_distance = record.get("home_distance_m")
                if (
                    record.get("component_id") != 1
                    or any(
                        not finite_number(value)
                        for value in (lat, lon, relative_alt, home_distance)
                    )
                    or not -90.0 <= float(lat) <= 90.0
                    or not -180.0 <= float(lon) <= 180.0
                    or not -20.0 <= float(relative_alt) <= 100.0
                    or not 0.0 <= float(home_distance) <= 500.0
                ):
                    failures.append(f"{name}: raw MAVLink pose is malformed or out of bounds")
                    continue
                raw_pose_values.append((float(relative_alt), float(home_distance)))
                if abs(float(lat)) >= 1.0 and abs(float(lon)) >= 1.0:
                    raw_valid_home_count += 1
            if raw_pose_values:
                raw_min_alt = min(value[0] for value in raw_pose_values)
                raw_max_alt = max(value[0] for value in raw_pose_values)
                raw_max_home_distance = max(value[1] for value in raw_pose_values)
                for key, derived in (
                    ("mavlink_minimum_relative_alt_m", raw_min_alt),
                    ("mavlink_maximum_relative_alt_m", raw_max_alt),
                    ("mavlink_maximum_home_distance_m", raw_max_home_distance),
                ):
                    if not finite_number(item.get(key)) or not math.isclose(
                        float(item[key]), derived, rel_tol=0.0, abs_tol=1e-6
                    ):
                        failures.append(f"{name}: {key} differs from raw MAVLink poses")
            if item.get("mavlink_valid_home_position_count") != raw_valid_home_count:
                failures.append(f"{name}: valid-home count differs from raw MAVLink poses")
            odom_arrival = sequence_metrics_ns(
                [record.get("monotonic_ns") for record in odom_events]
            )
            odom_sim = sequence_metrics_ns([record.get("stamp_ns") for record in odom_events])
            heartbeat_arrival = sequence_metrics_ns(
                [record.get("monotonic_ns") for record in heartbeat_events]
            )
            heartbeat_sim = sequence_metrics_ns(
                [record.get("sim_time_ns") for record in heartbeat_events]
            )
            if not all((odom_arrival, odom_sim, heartbeat_arrival, heartbeat_sim)):
                failures.append(f"{name}: raw sample timestamps are missing or nonadvancing")
            elif measurement_start_ns is not None and measurement_end_ns is not None:
                raw_odom_start = (odom_arrival["first_ns"] - measurement_start_ns) / 1_000_000_000
                raw_odom_age = (measurement_end_ns - odom_arrival["last_ns"]) / 1_000_000_000
                raw_heartbeat_age = (
                    measurement_end_ns - heartbeat_arrival["last_ns"]
                ) / 1_000_000_000
                raw_heartbeat_sim_start = (
                    heartbeat_sim["first_ns"] - odom_sim["first_ns"]
                ) / 1_000_000_000
                raw_heartbeat_sim_age = (
                    odom_sim["last_ns"] - heartbeat_sim["last_ns"]
                ) / 1_000_000_000
                raw_realtime_factor = (
                    (odom_sim["last_ns"] - odom_sim["first_ns"])
                    / (odom_arrival["last_ns"] - odom_arrival["first_ns"])
                )
                raw_checks = (
                    ("odometry start", raw_odom_start, 0.0, 1.0),
                    ("odometry age", raw_odom_age, 0.0, 1.0),
                    ("odometry arrival gap", odom_arrival["max_gap_s"], 0.0, 1.0),
                    ("odometry arrival rate", odom_arrival["rate_hz"], 5.0, float("inf")),
                    ("heartbeat wall age", raw_heartbeat_age, 0.0, 15.0),
                    ("heartbeat wall gap", heartbeat_arrival["max_gap_s"], 0.0, 15.0),
                    ("heartbeat simulated start", raw_heartbeat_sim_start, -0.1, 3.0),
                    ("heartbeat simulated age", raw_heartbeat_sim_age, 0.0, 3.0),
                    ("heartbeat simulated gap", heartbeat_sim["max_gap_s"], 0.0, 3.0),
                    ("heartbeat simulated rate", heartbeat_sim["rate_hz"], 0.8, float("inf")),
                    ("realtime factor", raw_realtime_factor, 0.1, 1.1),
                )
                for label, value, minimum, maximum in raw_checks:
                    if not math.isfinite(value) or not minimum <= value <= maximum:
                        failures.append(
                            f"{name}: raw {label}={value:.6g} is outside [{minimum}, {maximum}]"
                        )
    if (
        len(sysids) != 5
        or any(not isinstance(system_id, int) for system_id in sysids)
        or sorted(sysids) != [1, 2, 3, 4, 5]
    ):
        failures.append(f"system IDs are {sysids}")
    if (
        len(dds_ports) != 5
        or any(not isinstance(port, int) for port in dds_ports)
        or sorted(dds_ports) != list(range(2019, 2024))
    ):
        failures.append(f"DDS ports are not five unique values: {dds_ports}")
    if data.get("errors"):
        failures.append("five-UAV health record reports errors")
    allowed_m1_events = {
        "health_probe_start",
        "health_probe_failed",
        "heartbeat_endpoint_failed",
        "heartbeat_worker_failed",
        "heartbeat_unstamped",
        "readiness_odometry",
        "readiness_heartbeat",
        "readiness_mavlink_global_position",
        "readiness_process_sample",
        "robot_description_probe",
        "readiness",
        "measurement_start",
        "clock_correlation",
        "odometry",
        "heartbeat",
        "mavlink_global_position",
        "process_sample",
        "health_probe_interrupted",
        "gazebo_scene_probe",
        "gazebo_scene_probe_failed",
        "health_probe_complete",
    }
    unexpected_events = sorted(
        {
            str(record.get("event"))
            for record in records
            if record.get("event") not in allowed_m1_events
        }
    )
    if unexpected_events:
        failures.append(f"raw M1 contains unexpected event types: {unexpected_events}")
    fatal_runtime_events = {
        "health_probe_failed",
        "heartbeat_endpoint_failed",
        "heartbeat_worker_failed",
        "health_probe_interrupted",
        "gazebo_scene_probe_failed",
    }
    observed_fatal_events = sorted(
        {str(record.get("event")) for record in records if record.get("event") in fatal_runtime_events}
    )
    if observed_fatal_events:
        failures.append(f"raw M1 contains fatal lifecycle events: {observed_fatal_events}")
    for record in records:
        event_name = str(record.get("event") or "")
        if (
            event_name in {
                "measurement_start",
                "clock_correlation",
                "odometry",
                "heartbeat",
                "mavlink_global_position",
                "process_sample",
                "health_probe_interrupted",
            }
            and record.get("phase") != "measurement"
        ):
            failures.append(f"raw M1 event {event_name!r} is outside measurement phase")
        if (
            event_name.startswith("readiness")
            or event_name
            in {"health_probe_start", "health_probe_failed", "robot_description_probe"}
        ) and record.get("phase") != "readiness":
            failures.append(f"raw M1 event {event_name!r} is outside readiness phase")
        if event_name in {
            "gazebo_scene_probe",
            "gazebo_scene_probe_failed",
            "health_probe_complete",
        } and record.get("phase") != "finalization":
            failures.append(f"raw M1 event {event_name!r} is outside finalization phase")

    process_health = (
        data.get("process_health") if isinstance(data.get("process_health"), dict) else {}
    )
    process_group = process_health.get("process_group")
    if not nonnegative_integer(process_group) or process_group <= 0:
        failures.append("process-health supervised process group is invalid")
    if process_health.get("required_exact_counts") != M1_REQUIRED_PROCESS_COUNTS:
        failures.append("process-health exact-count contract is invalid")
    process_events = [record for record in records if record.get("event") == "process_sample"]
    if process_health.get("samples") != len(process_events) or not process_events:
        failures.append("process-health raw samples are missing or count-mismatched")
    process_timestamps = [record.get("monotonic_ns") for record in process_events]
    process_timing = sequence_metrics_ns(process_timestamps)
    derived_first_delay: float | None = None
    derived_last_age: float | None = None
    if process_timing is None:
        failures.append("process-health sample clocks are missing or nonadvancing")
    elif measurement_start_ns is not None and measurement_end_ns is not None:
        derived_first_delay = (
            process_timing["first_ns"] - measurement_start_ns
        ) / 1_000_000_000
        derived_last_age = (
            measurement_end_ns - process_timing["last_ns"]
        ) / 1_000_000_000
        if (
            not 0.0 <= derived_first_delay <= M1_PROCESS_SAMPLE_MAX_GAP_S
            or not 0.0 <= derived_last_age <= M1_PROCESS_SAMPLE_MAX_GAP_S
            or process_timing["max_gap_s"] > M1_PROCESS_SAMPLE_MAX_GAP_S
        ):
            failures.append(
                "process-health samples exceed the "
                f"{M1_PROCESS_SAMPLE_MAX_GAP_S:g}-second measurement continuity bound"
            )
        if (
            readiness_completed_ns is None
            or measurement_start_ns < readiness_completed_ns
            or process_timing["first_ns"] < measurement_start_ns
            or (
                process_timing["first_ns"] - readiness_completed_ns
            )
            / 1_000_000_000
            > M1_PROCESS_SAMPLE_MAX_GAP_S
        ):
            failures.append(
                "readiness completion to first measurement process sample exceeds the "
                f"{M1_PROCESS_SAMPLE_MAX_GAP_S:g}-second continuity bound"
            )
        for key, derived in (
            ("first_sample_delay_s", derived_first_delay),
            ("last_sample_age_s", derived_last_age),
            ("maximum_sample_gap_s", process_timing["max_gap_s"]),
        ):
            summarized = process_health.get(key)
            if not finite_number(summarized) or not math.isclose(
                float(summarized), float(derived), rel_tol=0.0, abs_tol=0.1
            ):
                failures.append(f"process-health summarized {key} differs from raw clocks")

    counts_by_role: dict[str, list[int]] = {
        role: [] for role in M1_REQUIRED_PROCESS_COUNTS
    }
    baseline_by_role: dict[str, set[str]] | None = None
    baseline_all: set[str] | None = None
    for sample_index, sample in enumerate(process_events):
        processes = sample.get("processes")
        if not isinstance(processes, list) or not processes:
            failures.append(f"process_sample[{sample_index}] lacks raw process identities")
            for role in counts_by_role:
                counts_by_role[role].append(0)
            continue
        if sample.get("phase") != "measurement":
            failures.append(f"process_sample[{sample_index}] is outside measurement phase")
        if sample.get("error") not in (None, ""):
            failures.append(f"process_sample[{sample_index}] reports a sampling error")
        pids = [process.get("pid") for process in processes if isinstance(process, dict)]
        if len(pids) != len(processes) or len(set(pids)) != len(pids):
            failures.append(f"process_sample[{sample_index}] has duplicate or invalid PIDs")

        derived_counts = {role: 0 for role in M1_REQUIRED_PROCESS_COUNTS}
        role_digests = {role: set() for role in M1_REQUIRED_PROCESS_COUNTS}
        all_digests: set[str] = set()
        for process_index, process in enumerate(processes):
            context = f"process_sample[{sample_index}].processes[{process_index}]"
            if not isinstance(process, dict):
                failures.append(f"{context} is not an object")
                continue
            try:
                cmdline_raw = base64.b64decode(
                    str(process.get("cmdline_b64") or ""), validate=True
                )
            except (ValueError, TypeError):
                cmdline_raw = b""
                failures.append(f"{context}.cmdline_b64 is invalid")
            argv = _m1_decode_cmdline(cmdline_raw)
            if hashlib.sha256(cmdline_raw).hexdigest() != process.get("cmdline_sha256"):
                failures.append(f"{context}.cmdline_sha256 does not match raw cmdline")
            if process.get("cmdline") != argv:
                failures.append(f"{context}.cmdline does not match raw cmdline")
            if not isinstance(process.get("command"), str) or not isinstance(
                process.get("arguments"), str
            ):
                failures.append(f"{context} lacks human-readable command arguments")
            role = _m1_process_role(process, argv)
            if process.get("role") != role:
                failures.append(f"{context}.role does not match independently decoded argv")
            failures.extend(
                _m1_locked_process_identity_failures(
                    process, argv, role, runtime_identity, context
                )
            )
            if role in derived_counts:
                derived_counts[role] += 1
            state = process.get("state")
            if state == "Z":
                failures.append(f"{context} is a zombie process")
            elif state not in M1_ALLOWED_PROCESS_STATES:
                failures.append(f"{context} has unhealthy process state {state!r}")
            if process.get("identity_errors") != []:
                failures.append(f"{context} has incomplete process identity")
            for key, minimum in (
                ("pid", 1),
                ("ppid", 0),
                ("pgid", 1),
                ("session_id", 1),
                ("start_ticks", 1),
                ("uid", 0),
            ):
                value = process.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                    failures.append(f"{context}.{key} is invalid")
            if process.get("pgid") != process_group or process.get("session_id") != process_group:
                failures.append(f"{context} escaped the supervised process group/session")
            if not isinstance(process.get("exe_path"), str) or not str(
                process.get("exe_path")
            ).startswith("/"):
                failures.append(f"{context}.exe_path is invalid")
            for key in ("cmdline_sha256", "exe_sha256"):
                if re.fullmatch(r"[0-9a-f]{64}", str(process.get(key) or "")) is None:
                    failures.append(f"{context}.{key} is not SHA-256")
            namespaces = process.get("namespaces")
            if not isinstance(namespaces, dict) or set(namespaces) != set(
                M1_PROCESS_NAMESPACES
            ):
                failures.append(f"{context}.namespaces is incomplete")
            else:
                for name in M1_PROCESS_NAMESPACES:
                    if re.fullmatch(rf"{name}:\[[0-9]+\]", str(namespaces.get(name) or "")) is None:
                        failures.append(f"{context}.namespaces.{name} is invalid")
            capabilities = process.get("capabilities")
            if not isinstance(capabilities, dict) or set(capabilities) != set(
                M1_CAPABILITIES
            ):
                failures.append(f"{context}.capabilities is incomplete")
            elif any(
                re.fullmatch(r"[0-9a-f]{16}", str(value or "")) is None
                for value in capabilities.values()
            ):
                failures.append(f"{context}.capabilities is invalid")
            if role == "arducopter" and "--serial2" in argv:
                failures.append(f"{context} configured SERIAL2 in the M1 component profile")
            identity_digest = _m1_process_identity_digest(process)
            all_digests.add(identity_digest)
            if role in role_digests:
                role_digests[role].add(identity_digest)

        if sample.get("counts") != derived_counts:
            failures.append(f"process_sample[{sample_index}] producer counts differ from raw identities")
        for role, required in M1_REQUIRED_PROCESS_COUNTS.items():
            observed = derived_counts[role]
            counts_by_role[role].append(observed)
            if observed != required:
                failures.append(
                    f"process_sample[{sample_index}] has {observed} {role}; expected exactly {required}"
                )
        if final_readiness_role_digests is None or final_readiness_all_digests is None:
            failures.append(
                f"process_sample[{sample_index}] cannot bind to final stable readiness identities"
            )
        else:
            for role in M1_REQUIRED_PROCESS_COUNTS:
                if role_digests[role] != final_readiness_role_digests[role]:
                    failures.append(
                        f"process_sample[{sample_index}] {role} identity set differs from "
                        "final stable readiness"
                    )
            if all_digests != final_readiness_all_digests:
                failures.append(
                    f"process_sample[{sample_index}] full identity set differs from "
                    "final stable readiness"
                )
        if baseline_by_role is None:
            baseline_by_role = role_digests
            baseline_all = all_digests
        else:
            for role in M1_REQUIRED_PROCESS_COUNTS:
                if role_digests[role] != baseline_by_role[role]:
                    failures.append(f"process_sample[{sample_index}] changed {role} identity set")
            if all_digests != baseline_all:
                failures.append(f"process_sample[{sample_index}] changed launch process identity set")

    derived_ranges = {
        role: {
            "minimum": min(values, default=0),
            "maximum": max(values, default=0),
        }
        for role, values in counts_by_role.items()
    }
    derived_endpoints, endpoint_failures = _m1_runtime_endpoints(
        process_events[0].get("processes", []) if process_events else []
    )
    failures.extend(endpoint_failures)
    if process_health.get("runtime_endpoints") != derived_endpoints:
        failures.append("process-health runtime endpoint matrix differs from raw argv")
    if process_health.get("observed_count_ranges") != derived_ranges:
        failures.append("process-health count ranges differ from raw process identities")
    derived_role_hashes = {
        role: _m1_identity_set_sha256((baseline_by_role or {}).get(role, set()))
        for role in M1_REQUIRED_PROCESS_COUNTS
    }
    if process_health.get("stable_identity_set_sha256") != derived_role_hashes:
        failures.append("process-health role identity hashes differ from raw process identities")
    derived_all_hash = (
        _m1_identity_set_sha256(baseline_all) if baseline_all is not None else None
    )
    if process_health.get("all_process_identity_set_sha256") != derived_all_hash:
        failures.append("process-health full identity hash differs from raw process identities")
    return gate(
        "passed" if not failures else "failed",
        "five-UAV health evidence evaluated",
        {
            "failures": list(failures),
            "failure_count": failures.total,
            "failures_truncated": failures.total > len(failures),
        },
    )


def no_bypass_status(run_dir: Path) -> dict[str, Any]:
    path, data = _structured_file(run_dir, "logs/no_bypass_active.json")
    if not data:
        smoke_path = run_dir / "logs" / "no_bypass.log"
        smoke = smoke_path.read_text(errors="replace") if smoke_path.is_file() else ""
        note = " smoke explicitly says active proof is missing" if "full P0 no-bypass proof still requires" in smoke else ""
        return gate("not_run", f"structured active on/stopped/recovery proof is missing;{note}".rstrip(";"))
    failures: list[str] = []
    if data.get("schema_version") != 2:
        failures.append("active no-bypass evidence schema_version is not 2")
    provenance = load_json(run_dir / "metrics/provenance.json")
    joint_runtime = load_json(run_dir / "metrics/joint_runtime.json")
    runtime_id = data.get("runtime_id")
    source_hash = data.get("source_hash")
    run_nonce = data.get("run_nonce")
    if data.get("run_id") != run_dir.name:
        failures.append("no-bypass run_id does not match")
    if runtime_id != joint_runtime.get("runtime_id"):
        failures.append("no-bypass runtime_id does not match joint runtime")
    if source_hash != provenance.get("source_hash"):
        failures.append("no-bypass source_hash does not match provenance")
    if not isinstance(run_nonce, str) or not 8 <= len(run_nonce) <= 48:
        failures.append("no-bypass run_nonce is missing or invalid")
    if data.get("errors") not in (None, []):
        failures.append("no-bypass summary reports errors")
    if data.get("raw_event_log") != NO_BYPASS_EVENT_LOG:
        failures.append(f"no-bypass raw_event_log must be {NO_BYPASS_EVENT_LOG}")
    raw_path, raw_error = checked_run_file(run_dir, NO_BYPASS_EVENT_LOG)
    events: list[dict[str, Any]] = []
    if raw_error:
        failures.append(raw_error)
    elif raw_path is not None:
        if data.get("raw_event_sha256") != sha256_file(raw_path):
            failures.append("no-bypass raw-event hash mismatch")
        events, raw_failures = load_jsonl(raw_path)
        failures.extend(raw_failures)
        failures.extend(
            raw_event_envelope_failures(
                events,
                run_id=run_dir.name,
                runtime_id=runtime_id,
                source_hash=source_hash,
            )
        )

    starts = [event for event in events if event.get("event") == "experiment_start"]
    completions = [event for event in events if event.get("event") == "experiment_complete"]
    if len(starts) != 1 or len(completions) != 1 or not events:
        failures.append("no-bypass raw log needs one experiment start and completion")
    else:
        if events[0] is not starts[0] or events[-1] is not completions[0]:
            failures.append("no-bypass start/completion do not bound the raw log")
        if completions[0].get("errors") not in (None, []):
            failures.append("no-bypass completion reports errors")

    def valid_hash(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    def valid_process_map(value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        for role, identity in value.items():
            if not isinstance(role, str) or not role or not isinstance(identity, dict):
                return False
            if (
                not nonnegative_integer(identity.get("pid"))
                or identity.get("pid", 0) <= 1
                or not nonnegative_integer(identity.get("start_ticks"))
                or identity.get("start_ticks", 0) <= 0
                or not valid_hash(identity.get("cmdline_sha256"))
            ):
                return False
        return True

    requirements = {"ns3_on": 10, "ns3_stopped": 5, "ns3_recovered": 10}
    phase_details: dict[str, Any] = {}
    stable_processes: dict[str, Any] | None = None
    ns3_identities: dict[str, tuple[Any, Any, Any]] = {}
    all_request_hashes: set[str] = set()
    phase_ranges: list[tuple[int, int]] = []
    for phase, required_attempts in requirements.items():
        phase_events = [event for event in events if event.get("phase") == phase]
        if not phase_events:
            failures.append(f"{phase}: phase has no raw events")
            continue
        phase_sequences = [
            event.get("event_seq")
            for event in phase_events
            if nonnegative_integer(event.get("event_seq"))
        ]
        if len(phase_sequences) != len(phase_events):
            failures.append(f"{phase}: phase event sequence is invalid")
        elif phase_sequences:
            phase_ranges.append((min(phase_sequences), max(phase_sequences)))
        attempts = [event for event in phase_events if event.get("event") == "command_attempt"]
        acknowledgements = [event for event in phase_events if event.get("event") == "command_ack"]
        timeouts = [event for event in phase_events if event.get("event") == "command_timeout"]
        heartbeats = [event for event in phase_events if event.get("event") == "heartbeat"]
        phase_details[phase] = {
            "attempts": len(attempts),
            "acknowledgements": len(acknowledgements),
            "timeouts": len(timeouts),
            "heartbeats": len(heartbeats),
        }
        if len(attempts) != required_attempts:
            failures.append(f"{phase}: needs exactly {required_attempts} decoded command attempts")
        attempt_by_hash: dict[str, dict[str, Any]] = {}
        for expected_attempt, attempt in enumerate(attempts, start=1):
            request_hash = attempt.get("request_sha256")
            expected_nonce = f"{run_nonce}:{phase}:{expected_attempt}"
            if attempt.get("attempt") != expected_attempt:
                failures.append(f"{phase}: command attempt numbering is not contiguous")
            if attempt.get("nonce") != expected_nonce:
                failures.append(f"{phase}: command nonce does not match attempt identity")
            if not valid_hash(request_hash) or request_hash in all_request_hashes:
                failures.append(f"{phase}: command request hash is invalid or reused")
            elif isinstance(request_hash, str):
                all_request_hashes.add(request_hash)
                attempt_by_hash[request_hash] = attempt
            if not valid_hash(attempt.get("marker_sha256")):
                failures.append(f"{phase}: STATUSTEXT marker hash is invalid")
            if (
                not nonnegative_integer(attempt.get("mavlink_seq"))
                or attempt.get("mavlink_seq", 256) > 255
                or not nonnegative_integer(attempt.get("target_system"))
                or not 1 <= attempt.get("target_system", 0) <= 255
                or not nonnegative_integer(attempt.get("mavlink_command"))
            ):
                failures.append(f"{phase}: decoded MAVLink request fields are invalid")

        ack_by_request: dict[str, list[dict[str, Any]]] = {}
        for acknowledgement in acknowledgements:
            request_hash = acknowledgement.get("request_sha256")
            ack_by_request.setdefault(str(request_hash), []).append(acknowledgement)
            request = attempt_by_hash.get(request_hash) if isinstance(request_hash, str) else None
            if request is None:
                failures.append(f"{phase}: ACK references an unknown request hash")
                continue
            if (
                acknowledgement.get("attempt") != request.get("attempt")
                or acknowledgement.get("nonce") != request.get("nonce")
                or acknowledgement.get("request_mavlink_seq") != request.get("mavlink_seq")
                or acknowledgement.get("source_system") != request.get("target_system")
                or acknowledgement.get("mavlink_command") != request.get("mavlink_command")
                or acknowledgement.get("mavlink_result") != 0
                or not valid_hash(acknowledgement.get("packet_sha256"))
            ):
                failures.append(f"{phase}: decoded ACK does not correlate to its request")

        timeout_hashes = {str(event.get("request_sha256")) for event in timeouts}
        health = [event for event in phase_events if event.get("event") == "endpoint_health"]
        if len(health) != 1 or health[0].get("all_live") is not True or not valid_process_map(
            health[0].get("stable_processes") if health else None
        ):
            failures.append(f"{phase}: stable endpoint process identity is not proven")
        elif stable_processes is None:
            stable_processes = health[0]["stable_processes"]
        elif health[0]["stable_processes"] != stable_processes:
            failures.append(f"{phase}: endpoint process identities changed")

        ns3_states = [event for event in phase_events if event.get("event") == "ns3_state"]
        if len(ns3_states) != 1:
            failures.append(f"{phase}: exactly one ns-3 process state is required")
        else:
            state = ns3_states[0]
            if phase == "ns3_stopped":
                if state.get("running") is not False:
                    failures.append("ns3_stopped: ns-3 is not proven absent")
                if any(state.get(key) not in (None, 0, "") for key in ("pid", "start_ticks", "cmdline_sha256")):
                    failures.append("ns3_stopped: stale live ns-3 identity is present")
            elif (
                state.get("running") is not True
                or not nonnegative_integer(state.get("pid"))
                or state.get("pid", 0) <= 1
                or not nonnegative_integer(state.get("start_ticks"))
                or state.get("start_ticks", 0) <= 0
                or not valid_hash(state.get("cmdline_sha256"))
            ):
                failures.append(f"{phase}: live ns-3 process identity is invalid")
            else:
                ns3_identities[phase] = (
                    state.get("pid"),
                    state.get("start_ticks"),
                    state.get("cmdline_sha256"),
                )

        if phase == "ns3_stopped":
            if acknowledgements or heartbeats:
                failures.append("ns3_stopped: ACK or heartbeat crossed while ns-3 was absent")
            if len(timeouts) != len(attempt_by_hash) or timeout_hashes != set(attempt_by_hash):
                failures.append("ns3_stopped: every request must have one timeout")
            heartbeat_timeouts = [
                event for event in phase_events if event.get("event") == "heartbeat_timeout"
            ]
            if (
                len(heartbeat_timeouts) != 1
                or heartbeat_timeouts[0].get("timed_out") is not True
                or not finite_number(heartbeat_timeouts[0].get("timeout_s"))
                or float(heartbeat_timeouts[0].get("timeout_s", 0.0)) < 1.0
            ):
                failures.append("ns3_stopped: heartbeat timeout is not proven")
        else:
            if timeouts:
                failures.append(f"{phase}: successful phase contains command timeouts")
            if set(ack_by_request) != set(attempt_by_hash) or any(
                len(values) != 1 for values in ack_by_request.values()
            ):
                failures.append(f"{phase}: every request needs exactly one decoded ACK")
            if not heartbeats or any(
                not valid_hash(event.get("packet_sha256"))
                or not nonnegative_integer(event.get("source_system"))
                for event in heartbeats
            ):
                failures.append(f"{phase}: decoded heartbeat evidence is invalid")

    if len(phase_ranges) == 3 and any(
        current[1] >= following[0] for current, following in zip(phase_ranges, phase_ranges[1:])
    ):
        failures.append("no-bypass phases overlap or are not ordered on/stopped/recovered")
    if (
        ns3_identities.get("ns3_on") is not None
        and ns3_identities.get("ns3_on") == ns3_identities.get("ns3_recovered")
    ):
        failures.append("ns-3 recovery reused the stopped process identity")
    return gate(
        "passed" if not failures else "failed",
        "active no-bypass raw on/stopped/recovery phases evaluated",
        {"failures": failures, "phases": phase_details},
    )


def packet_provenance_status(run_dir: Path, pcap_result: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
    path, data = _structured_file(run_dir, "metrics/packet_provenance.json")
    failures = list(pcap_result.get("failures", [])) + list(delivery.get("failures", []))
    if not data:
        failures.append(f"{path.relative_to(run_dir)} is missing or invalid")
    else:
        if data.get("schema_version") != 2:
            failures.append("packet provenance schema_version is not 2")
        if data.get("run_id") != run_dir.name:
            failures.append("packet provenance run_id does not match")
        provenance = load_json(run_dir / "metrics/provenance.json")
        joint_runtime = load_json(run_dir / "metrics/joint_runtime.json")
        if data.get("runtime_id") != joint_runtime.get("runtime_id"):
            failures.append("packet provenance runtime_id does not match joint runtime")
        if data.get("source_hash") != provenance.get("source_hash"):
            failures.append("packet provenance source_hash does not match provenance")
        nonce = data.get("run_nonce")
        if not isinstance(nonce, str) or len(nonce) < 8:
            failures.append("run_nonce is missing or too short")
        runtime_id = data.get("runtime_id")
        source_hash = data.get("source_hash")

        def valid_packet_hash(value: Any) -> bool:
            return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

        classes = data.get("traffic_classes") if isinstance(data.get("traffic_classes"), dict) else {}
        for traffic_class in TRAFFIC_CLASSES:
            record = classes.get(traffic_class) if isinstance(classes.get(traffic_class), dict) else {}
            tx = record.get("tx")
            rx = record.get("rx")
            if not nonnegative_integer(tx) or tx <= 0 or not nonnegative_integer(rx) or rx <= 0:
                failures.append(f"{traffic_class}: provenance TX/RX is not positive")
            packet_keys = PACKET_KEYS[traffic_class]
            runtime_packets = (
                load_json(run_dir / "metrics/runtime_summary.json").get("packets") or {}
            )
            if tx != runtime_packets.get(packet_keys[0]) or rx != runtime_packets.get(packet_keys[1]):
                failures.append(f"{traffic_class}: provenance counters differ from runtime summary")
        capture_points = data.get("capture_points") if isinstance(data.get("capture_points"), dict) else {}
        required_points = {
            "gcs_ingress": "pcap/gcs_ingress.pcap",
            "ns3_ingress": "pcap/ns3_ingress.pcap",
            "ns3_egress": "pcap/ns3_egress.pcap",
            "uav_egress": "pcap/uav_egress.pcap",
        }
        observed_paths: list[str] = []
        capture_stats: dict[str, dict[str, Any]] = {}
        capture_file_hashes: list[str] = []
        for point, expected_relative in required_points.items():
            record = capture_points.get(point) if isinstance(capture_points.get(point), dict) else {}
            relative = record.get("path")
            observed_paths.append(str(relative))
            if relative != expected_relative:
                failures.append(f"capture point {point} path is not {expected_relative}")
                continue
            capture_path, capture_error = checked_run_file(run_dir, relative)
            if capture_error:
                failures.append(capture_error)
                continue
            if record.get("sha256") != sha256_file(capture_path):
                failures.append(f"capture point {point} hash mismatch")
            stats = pcap_stats(capture_path, nonce=nonce if isinstance(nonce, str) else None)
            capture_stats[point] = stats
            if isinstance(stats.get("sha256"), str):
                capture_file_hashes.append(stats["sha256"])
            if stats["parse_error"] or stats["truncated_packets"] or stats["data_packets"] <= 0:
                failures.append(f"capture point {point} is not a valid data-packet capture")
            if isinstance(nonce, str) and stats["nonce_hits"] <= 0:
                failures.append(f"capture point {point} lacks the run nonce")
        if len(set(observed_paths)) != len(required_points):
            failures.append("capture points do not reference four distinct PCAP files")
        if len(capture_file_hashes) == len(required_points) and len(set(capture_file_hashes)) != len(
            required_points
        ):
            failures.append("capture-point PCAP files are byte-identical")

        if data.get("raw_mavlink_log") != MAVLINK_TRANSACTION_LOG:
            failures.append(
                f"packet provenance raw_mavlink_log must be {MAVLINK_TRANSACTION_LOG}"
            )
        mavlink_path, mavlink_error = checked_run_file(run_dir, MAVLINK_TRANSACTION_LOG)
        mavlink_events: list[dict[str, Any]] = []
        if mavlink_error:
            failures.append(mavlink_error)
        elif mavlink_path is not None:
            if data.get("raw_mavlink_sha256") != sha256_file(mavlink_path):
                failures.append("MAVLink raw-event hash mismatch")
            mavlink_events, mavlink_failures = load_jsonl(mavlink_path)
            failures.extend(mavlink_failures)
            failures.extend(
                raw_event_envelope_failures(
                    mavlink_events,
                    run_id=run_dir.name,
                    runtime_id=runtime_id,
                    source_hash=source_hash,
                )
            )
        mavlink = data.get("mavlink") if isinstance(data.get("mavlink"), dict) else {}
        starts = [event for event in mavlink_events if event.get("event") == "transaction_start"]
        completions = [event for event in mavlink_events if event.get("event") == "transaction_complete"]
        if len(starts) != 1 or len(completions) != 1 or not mavlink_events:
            failures.append("MAVLink log needs one transaction start and completion")
        else:
            if mavlink_events[0] is not starts[0] or mavlink_events[-1] is not completions[0]:
                failures.append("MAVLink transaction boundaries do not enclose the raw log")
            if completions[0].get("errors") not in (None, []):
                failures.append("MAVLink transaction completion reports errors")

        attempts = [event for event in mavlink_events if event.get("event") == "command_attempt"]
        acknowledgements = [event for event in mavlink_events if event.get("event") == "command_ack"]
        telemetry_events = [event for event in mavlink_events if event.get("event") == "telemetry"]
        heartbeats = [event for event in mavlink_events if event.get("event") == "heartbeat"]
        attempt_by_hash: dict[str, dict[str, Any]] = {}
        marker_hashes: set[str] = set()
        target_systems: set[int] = set()
        observed_nonces: set[str] = set()
        for expected_attempt, attempt in enumerate(attempts, start=1):
            command_hash = attempt.get("command_sha256")
            marker_hash = attempt.get("marker_sha256")
            attempt_nonce = attempt.get("nonce")
            if attempt.get("attempt") != expected_attempt:
                failures.append("MAVLink command attempt numbering is not contiguous")
            if (
                not isinstance(attempt_nonce, str)
                or not isinstance(nonce, str)
                or not attempt_nonce.startswith(nonce + ":")
                or attempt_nonce in observed_nonces
            ):
                failures.append("MAVLink command attempt nonce is invalid or reused")
            elif isinstance(attempt_nonce, str):
                observed_nonces.add(attempt_nonce)
            if (
                not valid_packet_hash(command_hash)
                or command_hash in attempt_by_hash
                or not valid_packet_hash(marker_hash)
            ):
                failures.append("MAVLink command/STATUSTEXT frame hash is invalid or reused")
            else:
                attempt_by_hash[command_hash] = attempt
                marker_hashes.add(marker_hash)
            target = attempt.get("target_system")
            if not nonnegative_integer(target) or not 1 <= target <= 5:
                failures.append("MAVLink command target_system is outside 1..5")
            else:
                target_systems.add(target)
            if (
                not nonnegative_integer(attempt.get("mavlink_seq"))
                or attempt.get("mavlink_seq", 256) > 255
                or not nonnegative_integer(attempt.get("mavlink_command"))
            ):
                failures.append("MAVLink decoded command fields are invalid")
        if target_systems != {1, 2, 3, 4, 5}:
            failures.append("MAVLink command evidence does not cover target systems 1..5")

        timeout_ms = data.get("response_timeout_ms")
        if not finite_number(timeout_ms) or not 1 <= float(timeout_ms) <= 5000:
            failures.append("MAVLink response_timeout_ms is outside [1, 5000]")
            timeout_ns = 0
        else:
            timeout_ns = int(float(timeout_ms) * 1_000_000)

        required_payload_hashes = set(attempt_by_hash) | marker_hashes
        ack_by_request: dict[str, list[dict[str, Any]]] = {}
        for acknowledgement in acknowledgements:
            request_hash = acknowledgement.get("request_sha256")
            request = attempt_by_hash.get(request_hash) if isinstance(request_hash, str) else None
            ack_by_request.setdefault(str(request_hash), []).append(acknowledgement)
            if request is None:
                failures.append("MAVLink ACK references an unknown command frame")
                continue
            if (
                acknowledgement.get("request_mavlink_seq") != request.get("mavlink_seq")
                or acknowledgement.get("source_system") != request.get("target_system")
                or acknowledgement.get("mavlink_command") != request.get("mavlink_command")
                or acknowledgement.get("mavlink_result") != 0
                or not valid_packet_hash(acknowledgement.get("packet_sha256"))
            ):
                failures.append("MAVLink ACK does not decode/correlate to its command")
            else:
                required_payload_hashes.add(acknowledgement["packet_sha256"])
            if (
                timeout_ns <= 0
                or not nonnegative_integer(acknowledgement.get("monotonic_ns"))
                or not nonnegative_integer(request.get("monotonic_ns"))
                or not 0
                <= acknowledgement.get("monotonic_ns", 0) - request.get("monotonic_ns", 0)
                <= timeout_ns
            ):
                failures.append("MAVLink ACK exceeded the bounded response window")
        if set(ack_by_request) != set(attempt_by_hash) or any(
            len(values) != 1 for values in ack_by_request.values()
        ):
            failures.append("every MAVLink command needs exactly one decoded ACK")

        telemetry_by_request: dict[str, list[dict[str, Any]]] = {}
        for telemetry in telemetry_events:
            request_hash = telemetry.get("request_sha256")
            request = attempt_by_hash.get(request_hash) if isinstance(request_hash, str) else None
            telemetry_by_request.setdefault(str(request_hash), []).append(telemetry)
            if (
                request is None
                or telemetry.get("request_mavlink_seq") != request.get("mavlink_seq")
                or telemetry.get("source_system") != request.get("target_system")
                or not nonnegative_integer(telemetry.get("message_id"))
                or not valid_packet_hash(telemetry.get("packet_sha256"))
            ):
                failures.append("MAVLink telemetry does not decode/correlate to its command")
                continue
            required_payload_hashes.add(telemetry["packet_sha256"])
            if (
                timeout_ns <= 0
                or not nonnegative_integer(telemetry.get("monotonic_ns"))
                or not 0
                <= telemetry.get("monotonic_ns", 0) - request.get("monotonic_ns", 0)
                <= timeout_ns
            ):
                failures.append("MAVLink telemetry exceeded the bounded response window")
        if set(telemetry_by_request) != set(attempt_by_hash) or any(
            len(values) < 1 for values in telemetry_by_request.values()
        ):
            failures.append("every MAVLink command needs correlated return telemetry")

        heartbeat_systems: set[int] = set()
        for heartbeat in heartbeats:
            source_system = heartbeat.get("source_system")
            packet_hash = heartbeat.get("packet_sha256")
            if not nonnegative_integer(source_system) or not 1 <= source_system <= 5 or not valid_packet_hash(
                packet_hash
            ):
                failures.append("decoded MAVLink heartbeat record is invalid")
            else:
                heartbeat_systems.add(source_system)
                required_payload_hashes.add(packet_hash)
        if heartbeat_systems != {1, 2, 3, 4, 5}:
            failures.append("MAVLink heartbeat evidence does not cover source systems 1..5")

        event_counts = {
            "heartbeats": len(heartbeats),
            "command_acks": len(acknowledgements),
            "telemetry_messages": len(telemetry_events),
            "command_attempts": len(attempts),
        }
        for key, count in event_counts.items():
            if mavlink.get(key) != count:
                failures.append(f"MAVLink summary count {key} does not match raw events")

        for point, stats in capture_stats.items():
            observed_hashes = set(stats.get("payload_sha256", []))
            missing_hashes = sorted(required_payload_hashes - observed_hashes)
            if missing_hashes:
                failures.append(f"capture point {point} lacks {len(missing_hashes)} exact MAVLink payload(s)")
            nonce_hashes = set(stats.get("payload_sha256_with_nonce", []))
            missing_markers = sorted(marker_hashes - nonce_hashes)
            if missing_markers:
                failures.append(f"capture point {point} lacks nonce-bearing STATUSTEXT payload(s)")
    return gate("passed" if not failures else "failed", "packet provenance and delivery evaluated", {"failures": failures})


def online_sionna_status(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "logs" / "sionna_link_queries.jsonl"
    if not path.is_file():
        return gate("not_run", "Sionna query log is missing")
    requests = 0
    states = 0
    errors = 0
    mock = False
    node_state_seqs: set[Any] = set()
    query_ids: set[str] = set()
    response_ids: set[str] = set()
    metadata_records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return gate("failed", f"could not read Sionna query log: {exc}")
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        message_type = message.get("type")
        if message_type == "sionna_runtime_start" or record.get("event") == "sionna_runtime_start":
            metadata_records.append(message)
        if message_type == "link_query":
            requests += 1
            query_id = message.get("query_id") or message.get("request_id")
            if isinstance(query_id, str) and query_id:
                query_ids.add(query_id)
            if message.get("node_state_seq") is not None:
                node_state_seqs.add(message.get("node_state_seq"))
        elif message_type == "link_state" and message.get("links"):
            states += 1
            query_id = message.get("query_id") or message.get("request_id")
            if isinstance(query_id, str) and query_id:
                response_ids.add(query_id)
            if message.get("provider_id") != "tcp_jsonl_real_sionna":
                mock = True
        elif message_type == "error":
            errors += 1
        if message.get("test_only") is True or message.get("acceptance_eligible") is False:
            mock = True
        if str(message.get("source", "")).lower() in {"mock", "test_free_space"}:
            mock = True
    details = {
        "requests": requests,
        "states": states,
        "errors": errors,
        "mock_or_test": mock,
        "node_state_sequences": len(node_state_seqs),
        "query_ids": len(query_ids),
        "matched_query_ids": len(query_ids.intersection(response_ids)),
    }
    if requests < 2 or states < 2:
        return gate("failed", "online Sionna log lacks multiple runtime query/state updates", details)
    if errors or mock:
        return gate("failed", "Sionna evidence contains errors or test/mock mode", details)
    if len(metadata_records) != 1:
        return gate("failed", "Sionna log lacks one runtime identity record", details)
    metadata = metadata_records[0]
    provenance = load_json(run_dir / "metrics/provenance.json")
    joint_runtime = load_json(run_dir / "metrics/joint_runtime.json")
    if (
        metadata.get("run_id") != run_dir.name
        or metadata.get("runtime_id") != joint_runtime.get("runtime_id")
        or metadata.get("source_hash") != provenance.get("source_hash")
        or metadata.get("provider_id") != "tcp_jsonl_real_sionna"
        or metadata.get("acceptance_eligible") is not True
    ):
        return gate("failed", "Sionna runtime identity/provider metadata does not match", details)
    if len(node_state_seqs) < 2 or len(query_ids.intersection(response_ids)) < 2:
        return gate("failed", "Sionna queries are not correlated to changing node state and responses", details)
    return gate("passed", "multiple real Sionna runtime updates are present", details)


def _strict_number(
    record: dict[str, Any],
    key: str,
    failures: list[str],
    context: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    value = record.get(key)
    if not finite_number(value):
        failures.append(f"{context}.{key} must be a finite number (booleans are invalid)")
        return None
    result = float(value)
    if minimum is not None and result < minimum:
        failures.append(f"{context}.{key} is below {minimum}")
        return None
    if maximum is not None and result > maximum:
        failures.append(f"{context}.{key} exceeds {maximum}")
        return None
    return result


def _strict_integer(
    record: dict[str, Any],
    key: str,
    failures: list[str],
    context: str,
    *,
    minimum: int = 0,
) -> int | None:
    value = record.get(key)
    if not nonnegative_integer(value) or value < minimum:
        failures.append(f"{context}.{key} must be an integer >= {minimum} (booleans are invalid)")
        return None
    return value


def _strict_string(record: dict[str, Any], key: str, failures: list[str], context: str) -> str | None:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        failures.append(f"{context}.{key} must be a non-empty string")
        return None
    return value


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _delivery_metrics(
    records: list[dict[str, Any]], failures: list[str], label: str
) -> dict[str, Any] | None:
    if not records:
        failures.append(f"{label} has no raw delivery samples")
        return None
    tx_total = 0
    rx_total = 0
    latencies: list[float] = []
    valid = True
    for index, record in enumerate(records):
        context = f"{label}[{index}]"
        tx = _strict_integer(record, "tx_packets", failures, context, minimum=1)
        rx = _strict_integer(record, "rx_packets", failures, context)
        raw_latencies = record.get("latency_ms")
        sample_latencies: list[float] = []
        if not isinstance(raw_latencies, list):
            failures.append(f"{context}.latency_ms must be a list of per-received-packet samples")
            valid = False
        else:
            for sample_index, value in enumerate(raw_latencies):
                if not finite_number(value) or float(value) < 0:
                    failures.append(
                        f"{context}.latency_ms[{sample_index}] must be a finite nonnegative number"
                    )
                    valid = False
                else:
                    sample_latencies.append(float(value))
        if tx is None or rx is None:
            valid = False
            continue
        if rx > tx:
            failures.append(f"{context} has impossible counters: rx_packets exceeds tx_packets")
            valid = False
        if isinstance(raw_latencies, list) and len(raw_latencies) != rx:
            failures.append(f"{context}.latency_ms length must equal rx_packets")
            valid = False
        tx_total += tx
        rx_total += rx
        latencies.extend(sample_latencies)
    if not valid or tx_total <= 0 or rx_total > tx_total:
        return None
    return {
        "tx_packets": tx_total,
        "rx_packets": rx_total,
        "loss_rate": (tx_total - rx_total) / tx_total,
        "p50_ms": _nearest_rank(latencies, 0.50),
        "p95_ms": _nearest_rank(latencies, 0.95),
    }


def _loss_or_latency_effect(
    baseline: dict[str, Any],
    impaired: dict[str, Any],
    *,
    loss_delta: float,
    latency_ratio: float,
    latency_delta_ms: float,
) -> bool:
    if impaired["loss_rate"] - baseline["loss_rate"] >= loss_delta:
        return True
    baseline_p95 = baseline.get("p95_ms")
    impaired_p95 = impaired.get("p95_ms")
    return (
        finite_number(baseline_p95)
        and finite_number(impaired_p95)
        and float(impaired_p95) - float(baseline_p95) >= latency_delta_ms
        and float(impaired_p95) >= float(baseline_p95) * latency_ratio
    )


def _strict_raw_envelope(
    run_dir: Path, data: dict[str, Any], profile: str
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    failures: list[str] = []
    provenance = load_json(run_dir / "metrics/provenance.json")
    joint_runtime = load_json(run_dir / "metrics/joint_runtime.json")
    source_hash = data.get("source_hash")
    runtime_id = data.get("runtime_id")
    if data.get("schema_version") != 2:
        failures.append("summary schema_version is not 2")
    if data.get("run_id") != run_dir.name:
        failures.append("summary run_id does not match run directory")
    if not isinstance(runtime_id, str) or len(runtime_id) < 8:
        failures.append("summary runtime_id is missing")
    if runtime_id != joint_runtime.get("runtime_id"):
        failures.append("summary runtime_id does not match joint_runtime")
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        failures.append("summary source_hash is not SHA-256")
    if source_hash != provenance.get("source_hash"):
        failures.append("summary source_hash does not match provenance")
    raw_path, raw_error = checked_run_file(run_dir, data.get("raw_event_log"))
    records: list[dict[str, Any]] = []
    if raw_error:
        failures.append(raw_error)
    elif raw_path is not None:
        if data.get("raw_event_sha256") != sha256_file(raw_path):
            failures.append("raw event hash does not match")
        records, raw_failures = load_jsonl(raw_path)
        failures.extend(raw_failures)
    event_counts: dict[str, int] = {}
    sequences: list[Any] = []
    clocks: list[Any] = []
    for index, record in enumerate(records):
        context = f"raw[{index}]"
        event_type = record.get("event")
        if not isinstance(event_type, str) or not event_type:
            failures.append(f"{context}.event must be a non-empty string")
        else:
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        for key, expected in (
            ("schema_version", 2),
            ("run_id", run_dir.name),
            ("runtime_id", runtime_id),
            ("source_hash", source_hash),
            ("experiment", profile),
        ):
            if record.get(key) != expected:
                failures.append(f"{context}.{key} does not match the strict envelope")
        sequences.append(record.get("event_seq"))
        clocks.append(record.get("monotonic_ns"))
    if not records:
        failures.append("raw event log is empty")
    if sequence_metrics_ns(sequences) is None:
        failures.append("event_seq must contain strictly increasing nonnegative integers")
    if sequence_metrics_ns(clocks) is None:
        failures.append("monotonic_ns must contain strictly increasing nonnegative integers")
    starts = [record for record in records if record.get("event") == "experiment_start"]
    completions = [record for record in records if record.get("event") == "experiment_complete"]
    if len(starts) != 1 or len(completions) != 1:
        failures.append("raw experiment must have exactly one start and one completion")
    else:
        if records[0] is not starts[0]:
            failures.append("experiment_start must be the first raw event")
        if records[-1] is not completions[0]:
            failures.append("experiment_complete must be the last raw event")
        if completions[0].get("errors") not in (None, []):
            failures.append("raw completion reports errors")
    if data.get("errors") not in (None, []):
        failures.append("experiment summary reports errors")
    return records, event_counts, failures


def _event_records(records: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("event") == event]


def _profile_sionna(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    event_types = (
        "node_state",
        "sionna_query",
        "link_state_applied",
        "packet_outcome_baseline",
        "packet_outcome_impaired",
    )
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    for event_type in event_types:
        for index, record in enumerate(_event_records(records, event_type)):
            correlation = _strict_string(record, "correlation_id", failures, f"{event_type}[{index}]")
            if correlation is None:
                continue
            bucket = indexed.setdefault(correlation, {})
            if event_type in bucket:
                failures.append(f"duplicate {event_type} for correlation_id {correlation}")
            bucket[event_type] = record
    complete = [correlation for correlation, bucket in indexed.items() if set(bucket) == set(event_types)]
    if len(complete) < 2:
        failures.append("Sionna causality requires at least two complete, distinct correlations")
    if any(set(bucket) != set(event_types) for bucket in indexed.values()):
        failures.append("every Sionna correlation must contain the complete causal chain")
    changed = 0
    state_sequences: set[int] = set()
    state_samples: set[tuple[str, tuple[float, float, float]]] = set()
    applied_packets = 0
    pair_metrics: list[dict[str, Any]] = []
    for correlation in complete:
        bucket = indexed[correlation]
        node = bucket["node_state"]
        query = bucket["sionna_query"]
        applied = bucket["link_state_applied"]
        baseline_record = bucket["packet_outcome_baseline"]
        impaired_record = bucket["packet_outcome_impaired"]
        node_seq = _strict_integer(node, "node_state_seq", failures, f"node_state[{correlation}]")
        node_id = _strict_string(node, "node_id", failures, f"node_state[{correlation}]")
        position = _xyz(node, "position_m", failures, f"node_state[{correlation}]")
        query_seq = _strict_integer(query, "node_state_seq", failures, f"sionna_query[{correlation}]")
        query_id = _strict_string(query, "query_id", failures, f"sionna_query[{correlation}]")
        link_id = _strict_string(applied, "link_id", failures, f"link_state_applied[{correlation}]")
        if query.get("provider_id") != "tcp_jsonl_real_sionna":
            failures.append(f"Sionna query {correlation} does not use the real provider")
        if applied.get("provider_id") != "tcp_jsonl_real_sionna":
            failures.append(f"applied link state {correlation} does not use the real provider")
        if query_id is None or applied.get("query_id") != query_id:
            failures.append(f"applied link state {correlation} does not match its query_id")
        if node_seq is None or query_seq != node_seq:
            failures.append(f"Sionna query {correlation} does not reference its node state")
        else:
            state_sequences.add(node_seq)
        if node_id is None or query.get("node_id") != node_id:
            failures.append(f"Sionna query {correlation} does not match its node_id")
        elif position is not None:
            state_samples.add((node_id, tuple(position)))
        baseline_per = _strict_number(
            applied, "baseline_per", failures, f"link_state_applied[{correlation}]", minimum=0, maximum=1
        )
        applied_per = _strict_number(
            applied, "applied_per", failures, f"link_state_applied[{correlation}]", minimum=0, maximum=1
        )
        if baseline_per is not None and applied_per is not None and applied_per - baseline_per < 0.10:
            failures.append(f"applied PER change for {correlation} is below 0.10")
        for outcome_name, outcome in (
            ("baseline", baseline_record),
            ("impaired", impaired_record),
        ):
            if outcome.get("query_id") != query_id:
                failures.append(f"{outcome_name} packet outcome {correlation} does not match query_id")
            if outcome.get("link_id") != link_id:
                failures.append(f"{outcome_name} packet outcome {correlation} does not match link_id")
        baseline = _delivery_metrics([baseline_record], failures, f"baseline[{correlation}]")
        impaired = _delivery_metrics([impaired_record], failures, f"impaired[{correlation}]")
        if baseline and impaired:
            applied_packets += impaired["tx_packets"]
            effect = _loss_or_latency_effect(
                baseline, impaired, loss_delta=0.10, latency_ratio=1.25, latency_delta_ms=10.0
            )
            changed += int(effect)
            if not effect:
                failures.append(f"packet outcome effect for {correlation} is below threshold")
            pair_metrics.append({"correlation_id": correlation, "baseline": baseline, "impaired": impaired})
    if len(state_sequences) < 2:
        failures.append("Sionna correlations do not contain changing node_state_seq values")
    if len(state_samples) < 2:
        failures.append("Sionna correlations do not contain distinct finite node-state samples")
    return {"complete_correlations": len(complete), "changed_correlations": changed, "distinct_node_states": len(state_samples), "applied_packets": applied_packets, "pairs": pair_metrics}


def _profile_link_locality(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    names = (
        "target_link_baseline",
        "target_link_impaired",
        "control_link_baseline",
        "control_link_impaired",
    )
    groups = {name: _event_records(records, name) for name in names}
    metrics = {name: _delivery_metrics(group, failures, name) for name, group in groups.items()}
    link_ids: dict[str, set[str]] = {}
    for name, group in groups.items():
        values = {
            value
            for index, record in enumerate(group)
            if (value := _strict_string(record, "link_id", failures, f"{name}[{index}]"))
            is not None
        }
        if len(values) != 1:
            failures.append(f"{name} must identify exactly one non-empty link_id")
        else:
            link_ids[name] = values
    target_ids = link_ids.get("target_link_baseline", set()) | link_ids.get("target_link_impaired", set())
    control_ids = link_ids.get("control_link_baseline", set()) | link_ids.get("control_link_impaired", set())
    if len(target_ids) != 1 or len(control_ids) != 1 or target_ids == control_ids:
        failures.append("target and control samples must describe two stable, distinct links")
    if any(metric and metric["tx_packets"] < 20 for metric in metrics.values()):
        failures.append("each locality phase needs at least 20 transmitted packets")
    target_base = metrics["target_link_baseline"]
    target_bad = metrics["target_link_impaired"]
    control_base = metrics["control_link_baseline"]
    control_bad = metrics["control_link_impaired"]
    target_changed = bool(
        target_base
        and target_bad
        and _loss_or_latency_effect(
            target_base, target_bad, loss_delta=0.10, latency_ratio=1.20, latency_delta_ms=5.0
        )
    )
    if not target_changed:
        failures.append("target-link impairment effect is below threshold")
    control_stable = False
    if control_base and control_bad:
        p95_a = control_base.get("p95_ms")
        p95_b = control_bad.get("p95_ms")
        control_stable = (
            abs(control_bad["loss_rate"] - control_base["loss_rate"]) <= 0.02
            and finite_number(p95_a)
            and finite_number(p95_b)
            and abs(float(p95_b) - float(p95_a)) <= 5.0
        )
    if not control_stable:
        failures.append("control-link change exceeds loss=0.02 or p95=5 ms tolerance")
    return {"metrics": metrics, "target_changed": target_changed, "control_stable": control_stable}


def _queue_samples(records: list[dict[str, Any]], failures: list[str], minimum_count: int = 2) -> bool:
    queues = _event_records(records, "queue_sample")
    if len(queues) < minimum_count:
        failures.append(f"at least {minimum_count} queue samples are required")
        return False
    bounded = True
    for index, record in enumerate(queues):
        depth = _strict_integer(record, "queue_depth_packets", failures, f"queue_sample[{index}]")
        limit = _strict_integer(record, "queue_limit_packets", failures, f"queue_sample[{index}]", minimum=1)
        if depth is None or limit is None or depth > limit:
            if depth is not None and limit is not None:
                failures.append(f"queue_sample[{index}] depth exceeds its limit")
            bounded = False
    return bounded


def _profile_shared_medium(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    single_records = _event_records(records, "single_flow_sample")
    concurrent_records = _event_records(records, "concurrent_flow_sample")
    single = _delivery_metrics(single_records, failures, "single_flow")
    concurrent = _delivery_metrics(concurrent_records, failures, "concurrent_flow")
    flow_ids = {
        value
        for index, record in enumerate(concurrent_records)
        if (
            value := _strict_string(
                record, "flow_id", failures, f"concurrent_flow_sample[{index}]"
            )
        )
        is not None
    }
    if len(flow_ids) < 2:
        failures.append("concurrent run must contain at least two distinct flow_id values")
    capacities: list[float] = []
    medium_ids: set[str] = set()
    offered = 0.0
    derived_rx_bps = 0.0
    for name, group in (("single_flow_sample", single_records), ("concurrent_flow_sample", concurrent_records)):
        for index, record in enumerate(group):
            context = f"{name}[{index}]"
            medium_id = _strict_string(record, "medium_id", failures, context)
            capacity = _strict_number(record, "capacity_bps", failures, context, minimum=1)
            offer = _strict_number(record, "offered_bps", failures, context, minimum=0)
            size = _strict_integer(record, "packet_size_bytes", failures, context, minimum=1)
            duration = _strict_number(record, "duration_s", failures, context, minimum=1e-9)
            if capacity is not None:
                capacities.append(capacity)
            if medium_id is not None:
                medium_ids.add(medium_id)
            if name == "concurrent_flow_sample" and offer is not None:
                offered += offer
            if name == "concurrent_flow_sample" and size is not None and duration is not None:
                rx = record.get("rx_packets")
                if nonnegative_integer(rx):
                    derived_rx_bps += rx * size * 8.0 / duration
    capacity = capacities[0] if capacities else None
    if capacity is None or any(abs(value - capacity) > max(1e-6, capacity * 1e-9) for value in capacities):
        failures.append("shared-medium samples do not use one consistent capacity_bps")
    for index, record in enumerate(_event_records(records, "queue_sample")):
        medium_id = _strict_string(record, "medium_id", failures, f"queue_sample[{index}]")
        if medium_id is not None:
            medium_ids.add(medium_id)
    if len(medium_ids) != 1:
        failures.append("all contention traffic and queue samples must use one medium_id")
    offer_ratio = offered / capacity if capacity else None
    if offer_ratio is None or offer_ratio < 1.5:
        failures.append("concurrent offered load is below 1.5x medium capacity")
    if capacity is not None and derived_rx_bps > capacity * 1.05:
        failures.append("derived concurrent receive rate exceeds medium capacity by more than 5%")
    effect = bool(
        single
        and concurrent
        and _loss_or_latency_effect(
            single, concurrent, loss_delta=0.05, latency_ratio=1.25, latency_delta_ms=5.0
        )
    )
    if not effect:
        failures.append("concurrent-flow degradation is below the shared-medium threshold")
    bounded = _queue_samples(records, failures)
    return {"medium_id": next(iter(medium_ids)) if len(medium_ids) == 1 else None, "single": single, "concurrent": concurrent, "offered_capacity_ratio": offer_ratio, "derived_rx_bps": derived_rx_bps, "queues_bounded": bounded}


def _profile_priority(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    offers = _event_records(records, "overload_offer")
    capacity_values: list[float] = []
    offered_total = 0.0
    offered_classes: set[str] = set()
    medium_ids: set[str] = set()
    for index, record in enumerate(offers):
        context = f"overload_offer[{index}]"
        medium_id = _strict_string(record, "medium_id", failures, context)
        traffic_class = _strict_string(record, "traffic_class", failures, context)
        offer = _strict_number(record, "offered_bps", failures, context, minimum=0)
        capacity = _strict_number(record, "capacity_bps", failures, context, minimum=1)
        if traffic_class:
            offered_classes.add(traffic_class)
        if medium_id:
            medium_ids.add(medium_id)
        if offer is not None:
            offered_total += offer
        if capacity is not None:
            capacity_values.append(capacity)
    capacity = capacity_values[0] if capacity_values else None
    if capacity is None or any(abs(value - capacity) > max(1e-6, capacity * 1e-9) for value in capacity_values):
        failures.append("overload offers do not use one consistent capacity_bps")
    if not {"control", "payload"}.issubset(offered_classes):
        failures.append("overload offers must include control and payload classes")
    offer_ratio = offered_total / capacity if capacity else None
    if offer_ratio is None or offer_ratio < 2.0:
        failures.append("offered load is below 2x medium capacity")
    control_records = _event_records(records, "control_delivery")
    payload_records = _event_records(records, "payload_delivery")
    for event_name, expected_class, group in (
        ("control_delivery", "control", control_records),
        ("payload_delivery", "payload", payload_records),
    ):
        for index, record in enumerate(group):
            context = f"{event_name}[{index}]"
            medium_id = _strict_string(record, "medium_id", failures, context)
            if medium_id:
                medium_ids.add(medium_id)
            if record.get("traffic_class") != expected_class:
                failures.append(f"{context}.traffic_class must be {expected_class}")
    control = _delivery_metrics(control_records, failures, "control_delivery")
    payload = _delivery_metrics(payload_records, failures, "payload_delivery")
    control_within = bool(
        control
        and control["loss_rate"] <= 0.05
        and finite_number(control.get("p95_ms"))
        and float(control["p95_ms"]) <= 250.0
    )
    if not control_within:
        failures.append("derived control loss/p95 exceeds 0.05/250 ms")
    payload_first = bool(
        control
        and payload
        and _loss_or_latency_effect(
            control, payload, loss_delta=0.10, latency_ratio=1.50, latency_delta_ms=10.0
        )
    )
    if not payload_first:
        failures.append("payload is not measurably degraded before control")
    decisions = _event_records(records, "scheduler_decision")
    contested = 0
    wrong = 0
    for index, record in enumerate(decisions):
        context = f"scheduler_decision[{index}]"
        medium_id = _strict_string(record, "medium_id", failures, context)
        control_backlog = _strict_integer(record, "control_backlog_packets", failures, context, minimum=1)
        payload_backlog = _strict_integer(record, "payload_backlog_packets", failures, context, minimum=1)
        if record.get("queue_owner") != "ns3":
            failures.append(f"{context}.queue_owner is not ns3")
        if medium_id:
            medium_ids.add(medium_id)
        if control_backlog is not None and payload_backlog is not None:
            contested += 1
            wrong += int(record.get("selected_class") != "control")
    if contested < 3 or wrong:
        failures.append("need at least three contested ns-3 decisions, all selecting control")
    for index, record in enumerate(_event_records(records, "queue_sample")):
        medium_id = _strict_string(record, "medium_id", failures, f"queue_sample[{index}]")
        if medium_id:
            medium_ids.add(medium_id)
    if len(medium_ids) != 1:
        failures.append("all overload, delivery, scheduler, and queue samples must use one medium_id")
    bounded = _queue_samples(records, failures)
    return {"medium_id": next(iter(medium_ids)) if len(medium_ids) == 1 else None, "offered_capacity_ratio": offer_ratio, "control": control, "payload": payload, "control_within_threshold": control_within, "payload_degraded_first": payload_first, "contested_scheduler_decisions": contested, "queues_bounded": bounded}


def _profile_jamming(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    radio_names = {
        "off_before": "jammer_off_before",
        "on": "jammer_on",
        "off_after": "jammer_off_after",
    }
    radio: dict[str, dict[str, float | None]] = {}
    link_ids: set[str] = set()
    jammer_ids: set[str] = set()
    for phase, event_name in radio_names.items():
        group = _event_records(records, event_name)
        if len(group) < 2:
            failures.append(f"{event_name} requires at least two radio samples")
        sinr: list[float] = []
        js: list[float] = []
        for index, record in enumerate(group):
            link_id = _strict_string(record, "link_id", failures, f"{event_name}[{index}]")
            jammer_id = _strict_string(record, "jammer_id", failures, f"{event_name}[{index}]")
            sinr_value = _strict_number(record, "sinr_db", failures, f"{event_name}[{index}]")
            js_value = _strict_number(record, "js_db", failures, f"{event_name}[{index}]")
            if link_id is not None:
                link_ids.add(link_id)
            if jammer_id is not None:
                jammer_ids.add(jammer_id)
            if sinr_value is not None:
                sinr.append(sinr_value)
            if js_value is not None:
                js.append(js_value)
        radio[phase] = {"sinr_db": _median(sinr), "js_db": _median(js)}
    packet_records = _event_records(records, "packet_outcome")
    for index, record in enumerate(packet_records):
        link_id = _strict_string(record, "link_id", failures, f"packet_outcome[{index}]")
        if link_id is not None:
            link_ids.add(link_id)
    packets = {
        phase: _delivery_metrics(
            [record for record in packet_records if record.get("phase") == phase],
            failures,
            f"packet_outcome[{phase}]",
        )
        for phase in radio_names
    }
    unknown_phases = [record.get("phase") for record in packet_records if record.get("phase") not in radio_names]
    if unknown_phases:
        failures.append("packet_outcome contains an unknown jammer phase")
    if len(link_ids) != 1 or len(jammer_ids) != 1:
        failures.append("all jammer radio and packet samples must use one link_id and jammer_id")
    before = radio["off_before"]
    on = radio["on"]
    after = radio["off_after"]
    sinr_changed = bool(finite_number(before["sinr_db"]) and finite_number(on["sinr_db"]) and float(before["sinr_db"]) - float(on["sinr_db"]) >= 3.0)
    js_changed = bool(finite_number(before["js_db"]) and finite_number(on["js_db"]) and float(on["js_db"]) - float(before["js_db"]) >= 3.0)
    packet_changed = bool(packets["off_before"] and packets["on"] and _loss_or_latency_effect(packets["off_before"], packets["on"], loss_delta=0.10, latency_ratio=1.25, latency_delta_ms=10.0))
    recovered = False
    if packets["off_before"] and packets["off_after"]:
        before_p95 = packets["off_before"].get("p95_ms")
        after_p95 = packets["off_after"].get("p95_ms")
        recovered = (
            abs(packets["off_after"]["loss_rate"] - packets["off_before"]["loss_rate"]) <= 0.03
            and finite_number(before_p95)
            and finite_number(after_p95)
            and abs(float(after_p95) - float(before_p95)) <= 5.0
            and finite_number(before["sinr_db"])
            and finite_number(after["sinr_db"])
            and abs(float(after["sinr_db"]) - float(before["sinr_db"])) <= 2.0
            and finite_number(before["js_db"])
            and finite_number(after["js_db"])
            and abs(float(after["js_db"]) - float(before["js_db"])) <= 2.0
        )
    if not sinr_changed:
        failures.append("jammer-on median SINR drop is below 3 dB")
    if not js_changed:
        failures.append("jammer-on median J/S increase is below 3 dB")
    if not packet_changed:
        failures.append("jammer-on packet effect is below threshold")
    if not recovered:
        failures.append("off-after phase does not recover within radio/packet tolerances")
    return {"link_id": next(iter(link_ids)) if len(link_ids) == 1 else None, "jammer_id": next(iter(jammer_ids)) if len(jammer_ids) == 1 else None, "radio_medians": radio, "packet_metrics": packets, "sinr_changed": sinr_changed, "js_changed": js_changed, "packet_changed": packet_changed, "recovered": recovered}


def _profile_time_coherence(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    pose_by_seq: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(_event_records(records, "pose_sample")):
        pose_seq = _strict_integer(record, "pose_seq", failures, f"pose_sample[{index}]")
        if pose_seq is not None:
            if pose_seq in pose_by_seq:
                failures.append(f"duplicate pose_seq {pose_seq}")
            pose_by_seq[pose_seq] = record
    updates: dict[str, dict[str, Any]] = {}
    async_delays: list[int] = []
    for index, record in enumerate(_event_records(records, "sionna_update")):
        context = f"sionna_update[{index}]"
        update_id = _strict_string(record, "update_id", failures, context)
        pose_seq = _strict_integer(record, "pose_seq", failures, context)
        if update_id is not None:
            if update_id in updates:
                failures.append(f"duplicate update_id {update_id}")
            updates[update_id] = record
        pose = pose_by_seq.get(pose_seq) if pose_seq is not None else None
        if pose is None:
            failures.append(f"{context} references a missing pose_seq")
        else:
            delay = record["monotonic_ns"] - pose["monotonic_ns"]
            if delay <= 0:
                failures.append(f"{context} is not asynchronous after its pose")
            else:
                async_delays.append(delay)
    decisions = _event_records(records, "packet_decision")
    packet_ids: set[str] = set()
    stale = 0
    ages: list[int] = []
    for index, record in enumerate(decisions):
        context = f"packet_decision[{index}]"
        packet_id = _strict_string(record, "packet_id", failures, context)
        update_id = _strict_string(record, "update_id", failures, context)
        ttl = _strict_integer(record, "state_ttl_ns", failures, context, minimum=1)
        if packet_id is not None:
            if packet_id in packet_ids:
                failures.append(f"duplicate packet_id {packet_id}")
            packet_ids.add(packet_id)
        update = updates.get(update_id) if update_id else None
        if update is None:
            failures.append(f"{context} references a missing update_id")
        elif ttl is not None:
            age = record["monotonic_ns"] - update["monotonic_ns"]
            if age < 0:
                failures.append(f"{context} precedes its Sionna update")
            else:
                ages.append(age)
                stale += int(age > ttl)
    if len(pose_by_seq) < 2 or len(updates) < 2 or len(decisions) < 2:
        failures.append("time coherence needs at least two poses, updates, and packet decisions")
    late_ratio = stale / len(ages) if ages else None
    if stale:
        failures.append("packet decisions used expired Sionna state")
    realtime = _event_records(records, "realtime_sample")
    sim_times: list[int] = []
    wall_times: list[int] = []
    for index, record in enumerate(realtime):
        sim_time = _strict_integer(record, "sim_time_ns", failures, f"realtime_sample[{index}]")
        if sim_time is not None:
            sim_times.append(sim_time)
            wall_times.append(record["monotonic_ns"])
    factors: list[float] = []
    if len(sim_times) < 3 or sequence_metrics_ns(sim_times) is None:
        failures.append("at least three strictly increasing realtime sim_time_ns samples are required")
    else:
        for previous_sim, current_sim, previous_wall, current_wall in zip(
            sim_times, sim_times[1:], wall_times, wall_times[1:]
        ):
            factors.append((current_sim - previous_sim) / (current_wall - previous_wall))
        if min(factors) < 0.95 or max(factors) > 1.05:
            failures.append("derived per-interval realtime factor is outside [0.95, 1.05]")
    return {"pose_samples": len(pose_by_seq), "updates": len(updates), "packet_decisions": len(decisions), "async_delay_ns": async_delays, "state_age_ns": ages, "late_update_ratio": late_ratio, "realtime_factors": factors}


def _xyz(record: dict[str, Any], key: str, failures: list[str], context: str) -> list[float] | None:
    value = record.get(key)
    if not isinstance(value, list) or len(value) != 3 or any(not finite_number(item) for item in value):
        failures.append(f"{context}.{key} must be three finite numeric coordinates")
        return None
    return [float(item) for item in value]


def _profile_scene_alignment(records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    for index, record in enumerate(_event_records(records, "scene_hash")):
        context = f"scene_hash[{index}]"
        component = _strict_string(record, "component", failures, context)
        scene_hash = record.get("scene_sha256")
        if not isinstance(scene_hash, str) or re.fullmatch(r"[0-9a-f]{64}", scene_hash) is None:
            failures.append(f"{context}.scene_sha256 is not SHA-256")
        elif component:
            if component in hashes:
                failures.append(f"duplicate scene hash component {component}")
            hashes[component] = scene_hash
    if not {"gazebo", "sionna", "network"}.issubset(hashes) or len(set(hashes.values())) != 1:
        failures.append("gazebo, sionna, and network must report one shared scene SHA-256")
    contract_ids: set[str] = set()
    for index, record in enumerate(_event_records(records, "frame_contract")):
        context = f"frame_contract[{index}]"
        contract_id = _strict_string(record, "contract_id", failures, context)
        _strict_string(record, "source_frame", failures, context)
        _strict_string(record, "target_frame", failures, context)
        _xyz(record, "translation_m", failures, context)
        quaternion = record.get("rotation_xyzw")
        if not isinstance(quaternion, list) or len(quaternion) != 4 or any(not finite_number(item) for item in quaternion):
            failures.append(f"{context}.rotation_xyzw must be four finite numeric values")
        else:
            norm = math.sqrt(sum(float(item) ** 2 for item in quaternion))
            if not 0.99 <= norm <= 1.01:
                failures.append(f"{context}.rotation_xyzw is not a unit quaternion")
        if contract_id:
            contract_ids.add(contract_id)
    if not {"gazebo_to_sionna", "gazebo_enu_to_ardupilot_ned"}.issubset(contract_ids):
        failures.append("both required frame contracts are missing")
    landmark_ids: set[str] = set()
    errors: list[float] = []
    for index, record in enumerate(_event_records(records, "landmark_measurement")):
        context = f"landmark_measurement[{index}]"
        landmark_id = _strict_string(record, "landmark_id", failures, context)
        gazebo = _xyz(record, "gazebo_xyz_m", failures, context)
        sionna = _xyz(record, "sionna_xyz_m", failures, context)
        if landmark_id:
            if landmark_id in landmark_ids:
                failures.append(f"duplicate landmark_id {landmark_id}")
            landmark_ids.add(landmark_id)
        if gazebo and sionna:
            errors.append(math.sqrt(sum((left - right) ** 2 for left, right in zip(gazebo, sionna))))
    if len(landmark_ids) < 3:
        failures.append("at least three distinct landmark measurements are required")
    if not errors or max(errors) > 1.0:
        failures.append("derived landmark alignment error exceeds 1.0 m")
    return {"scene_sha256": next(iter(set(hashes.values()))) if hashes and len(set(hashes.values())) == 1 else None, "frame_contracts": sorted(contract_ids), "landmark_errors_m": errors, "max_landmark_error_m": max(errors) if errors else None}


def _profile_repeatability(run_dir: Path, records: list[dict[str, Any]], failures: list[str]) -> dict[str, Any]:
    clones = _event_records(records, "clean_clone_run")
    if len(clones) != 2:
        failures.append("repeatability requires exactly two clean_clone_run records")
    parent_provenance = load_json(run_dir / "metrics/provenance.json")
    child_ids: set[str] = set()
    child_runtime_ids: set[str] = set()
    validation_paths: set[str] = set()
    provenance_paths: set[str] = set()
    validation_hashes: set[str] = set()
    provenance_hashes: set[str] = set()
    revisions: set[tuple[Any, Any]] = set()
    children: list[dict[str, Any]] = []
    forbidden_packet_keys = {"tx_packets", "rx_packets", "latency_ms", "loss_rate", "packet_evidence"}
    required_child_gates = set(P0_GATE_IDS) - {"repeatability"}
    for index, record in enumerate(clones):
        context = f"clean_clone_run[{index}]"
        if forbidden_packet_keys.intersection(record):
            failures.append(f"{context} must not embed or aggregate child packet evidence")
        child_id = _strict_string(record, "child_run_id", failures, context)
        child_runtime = _strict_string(record, "child_runtime_id", failures, context)
        validation_relative = _strict_string(record, "validation_artifact", failures, context)
        provenance_relative = _strict_string(record, "provenance_artifact", failures, context)
        if child_id:
            child_ids.add(child_id)
        if child_runtime:
            child_runtime_ids.add(child_runtime)
        if validation_relative:
            validation_paths.add(validation_relative)
        if provenance_relative:
            provenance_paths.add(provenance_relative)
        validation_path, validation_error = checked_run_file(run_dir, validation_relative)
        provenance_path, provenance_error = checked_run_file(run_dir, provenance_relative)
        if validation_error:
            failures.append(f"{context}: {validation_error}")
        if provenance_error:
            failures.append(f"{context}: {provenance_error}")
        validation_hash = sha256_file(validation_path) if validation_path else None
        provenance_hash = sha256_file(provenance_path) if provenance_path else None
        if validation_hash != record.get("validation_sha256"):
            failures.append(f"{context} validation artifact hash does not match")
        if provenance_hash != record.get("provenance_sha256"):
            failures.append(f"{context} provenance artifact hash does not match")
        if isinstance(validation_hash, str):
            validation_hashes.add(validation_hash)
        if isinstance(provenance_hash, str):
            provenance_hashes.add(provenance_hash)
        child_validation = load_json(validation_path) if validation_path else {}
        child_provenance = load_json(provenance_path) if provenance_path else {}
        if child_validation.get("schema_version") != 2 or child_validation.get("validation_engine") != "network.validation.evidence":
            failures.append(f"{context} child validation artifact has the wrong schema/engine")
        if child_validation.get("run_id") != child_id or child_provenance.get("run_id") != child_id:
            failures.append(f"{context} child artifact run_id does not match")
        if child_provenance.get("schema_version") != 2:
            failures.append(f"{context} child provenance schema_version is not 2")
        if child_provenance.get("runtime_id") != child_runtime:
            failures.append(f"{context} child provenance runtime_id does not match")
        if child_provenance.get("git_dirty") is not False or child_provenance.get("git_status") != []:
            failures.append(f"{context} child checkout is not clean")
        child_commit = child_provenance.get("git_commit")
        child_source_hash = child_provenance.get("source_hash")
        revision = (child_commit, child_source_hash)
        valid_revision = (
            isinstance(child_commit, str)
            and re.fullmatch(r"[0-9a-f]{40}", child_commit) is not None
            and isinstance(child_source_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", child_source_hash) is not None
        )
        if not valid_revision:
            failures.append(f"{context} child revision hashes are malformed")
        else:
            revisions.add((child_commit, child_source_hash))
        if revision != (parent_provenance.get("git_commit"), parent_provenance.get("source_hash")):
            failures.append(f"{context} child revision differs from parent provenance")
        child_gates = child_validation.get("gates")
        child_p0 = child_gates.get("p0") if isinstance(child_gates, dict) else None
        if not isinstance(child_p0, dict) or not required_child_gates.issubset(child_p0):
            failures.append(f"{context} child validation lacks required independent P0 gates")
        elif any(child_p0[gate_id].get("status") != "passed" for gate_id in required_child_gates if isinstance(child_p0.get(gate_id), dict)) or any(not isinstance(child_p0.get(gate_id), dict) for gate_id in required_child_gates):
            failures.append(f"{context} child validation has a non-passing required P0 gate")
        children.append({"run_id": child_id, "runtime_id": child_runtime, "validation_sha256": validation_hash, "provenance_sha256": provenance_hash, "revision": revision})
    if len(child_ids) != 2 or len(child_runtime_ids) != 2:
        failures.append("repeatability children must have distinct run_id and runtime_id values")
    if len(validation_paths) != 2 or len(provenance_paths) != 2:
        failures.append("repeatability children must reference distinct artifact paths")
    if len(validation_hashes) != 2 or len(provenance_hashes) != 2:
        failures.append("repeatability child validation/provenance artifacts must have distinct hashes")
    if len(revisions) != 1:
        failures.append("repeatability children do not use one revision")
    return {"children": children, "revision_count": len(revisions)}


STRICT_PROFILE_EVENTS: dict[str, set[str]] = {
    "sionna_causality": {"node_state", "sionna_query", "link_state_applied", "packet_outcome_baseline", "packet_outcome_impaired"},
    "link_locality": {"target_link_baseline", "target_link_impaired", "control_link_baseline", "control_link_impaired"},
    "shared_medium": {"single_flow_sample", "concurrent_flow_sample", "queue_sample"},
    "priority": {"overload_offer", "control_delivery", "payload_delivery", "queue_sample", "scheduler_decision"},
    "jamming": {"jammer_off_before", "jammer_on", "jammer_off_after", "packet_outcome"},
    "time_coherence": {"sionna_update", "packet_decision", "pose_sample", "realtime_sample"},
    "scene_alignment": {"scene_hash", "frame_contract", "landmark_measurement"},
    "repeatability": {"clean_clone_run"},
}

STRICT_PROFILE_RAW_PATHS = {
    "sionna_causality": "logs/sionna_causality_events.jsonl",
    "link_locality": "logs/link_locality_events.jsonl",
    "shared_medium": "logs/contention_experiment_events.jsonl",
    "priority": "logs/priority_experiment_events.jsonl",
    "jamming": "logs/jammer_experiment_events.jsonl",
    "time_coherence": "logs/time_coherence_events.jsonl",
    "scene_alignment": "logs/scene_alignment_events.jsonl",
    "repeatability": "logs/repeatability_events.jsonl",
}


def _strict_raw_experiment(run_dir: Path, relative: str, title: str, profile: str) -> dict[str, Any]:
    path, data = _structured_file(run_dir, relative)
    if not data:
        return gate("not_run", f"{path.relative_to(run_dir)} is missing or invalid")
    if profile not in STRICT_PROFILE_EVENTS:
        return gate("failed", f"unknown strict raw-event profile: {profile}")
    records, event_counts, failures = _strict_raw_envelope(run_dir, data, profile)
    expected_raw_path = STRICT_PROFILE_RAW_PATHS[profile]
    if data.get("raw_event_log") != expected_raw_path:
        failures.append(
            f"raw_event_log must use the fixed strict-profile path {expected_raw_path}"
        )
    allowed = STRICT_PROFILE_EVENTS[profile] | {"experiment_start", "experiment_complete"}
    unexpected = sorted(
        {
            event_type
            for record in records
            if isinstance((event_type := record.get("event")), str)
            and event_type not in allowed
        }
    )
    if unexpected:
        failures.append("unexpected raw event types: " + ", ".join(map(str, unexpected)))
    before_profile_failures = len(failures)
    if before_profile_failures:
        # A profile must never try to interpret records whose identity/order is
        # invalid.  Besides being fail-closed, this prevents malformed clock
        # types from reaching arithmetic in the time-coherence derivation.
        derived: dict[str, Any] = {}
    elif profile == "sionna_causality":
        derived = _profile_sionna(records, failures)
    elif profile == "link_locality":
        derived = _profile_link_locality(records, failures)
    elif profile == "shared_medium":
        derived = _profile_shared_medium(records, failures)
    elif profile == "priority":
        derived = _profile_priority(records, failures)
    elif profile == "jamming":
        derived = _profile_jamming(records, failures)
    elif profile == "time_coherence":
        derived = _profile_time_coherence(records, failures)
    elif profile == "scene_alignment":
        derived = _profile_scene_alignment(records, failures)
    else:
        derived = _profile_repeatability(run_dir, records, failures)
    if profile == "scene_alignment" and derived.get("scene_sha256") is not None:
        if data.get("scene_hash") != derived["scene_sha256"]:
            failures.append("summary scene_hash does not match the raw shared scene hash")
    return gate(
        "passed" if not failures else "failed",
        f"{title} independently derived from strict raw-event profile",
        {
            "profile": profile,
            "failures": failures,
            "envelope_failures": failures[:before_profile_failures],
            "event_counts": event_counts,
            "derived": derived,
        },
    )


def _legacy_raw_experiment(
    run_dir: Path,
    relative: str,
    title: str,
    required_true: Iterable[str],
    required_event_types: Iterable[str] = (),
    numeric_minimums: dict[str, float] | None = None,
    numeric_maximums: dict[str, float] | None = None,
    minimum_event_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    path, data = _structured_file(run_dir, relative)
    if not data:
        return gate("not_run", f"{path.relative_to(run_dir)} is missing or invalid")
    failures: list[str] = []
    if data.get("schema_version") != 2:
        failures.append("schema_version is not 2")
    if data.get("run_id") != run_dir.name:
        failures.append("run_id does not match run directory")
    runtime_id = data.get("runtime_id")
    if not isinstance(runtime_id, str) or len(runtime_id) < 8:
        failures.append("runtime_id is missing")
    provenance = load_json(run_dir / "metrics/provenance.json")
    if data.get("source_hash") != provenance.get("source_hash"):
        failures.append("source_hash does not match provenance")
    raw_path, raw_error = checked_run_file(run_dir, data.get("raw_event_log"))
    raw_records: list[dict[str, Any]] = []
    if raw_error:
        failures.append(raw_error)
    elif raw_path is not None:
        if data.get("raw_event_sha256") != sha256_file(raw_path):
            failures.append("raw event hash does not match")
        raw_records, raw_failures = load_jsonl(raw_path)
        failures.extend(raw_failures)
    event_counts: dict[str, int] = {}
    for record in raw_records:
        event_type = record.get("event")
        if isinstance(event_type, str):
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
    for event_type in required_event_types:
        minimum_count = (minimum_event_counts or {}).get(event_type, 1)
        if event_counts.get(event_type, 0) < minimum_count:
            failures.append(f"raw event {event_type!r} count is below {minimum_count}")
    starts = [record for record in raw_records if record.get("event") == "experiment_start"]
    completions = [record for record in raw_records if record.get("event") == "experiment_complete"]
    if len(starts) != 1 or len(completions) != 1:
        failures.append("raw experiment must have exactly one start and one completion")
    else:
        for key, expected in (
            ("run_id", run_dir.name),
            ("runtime_id", runtime_id),
            ("source_hash", data.get("source_hash")),
        ):
            if starts[0].get(key) != expected:
                failures.append(f"experiment_start {key} does not match")
        if completions[0].get("errors"):
            failures.append("raw completion reports errors")
    for key in required_true:
        if data.get(key) is not True:
            failures.append(f"{key} is not true")
    for key, minimum in (numeric_minimums or {}).items():
        value = data.get(key)
        if not finite_number(value) or float(value) < minimum:
            failures.append(f"{key} is below {minimum}")
    for key, maximum in (numeric_maximums or {}).items():
        value = data.get(key)
        if not finite_number(value) or float(value) > maximum:
            failures.append(f"{key} exceeds {maximum}")
    if data.get("errors"):
        failures.append("experiment reports errors")
    return gate(
        "passed" if not failures else "failed",
        f"{title} raw-event evidence evaluated",
        {"failures": failures, "event_counts": event_counts},
    )


def _raw_experiment(
    run_dir: Path,
    relative: str,
    title: str,
    required_true: Iterable[str],
    required_event_types: Iterable[str] = (),
    numeric_minimums: dict[str, float] | None = None,
    numeric_maximums: dict[str, float] | None = None,
    minimum_event_counts: dict[str, int] | None = None,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Evaluate raw experiment evidence.

    The historical positional API remains available for non-P0 callers.  P0
    callers select a strict profile; in that branch summary booleans and
    ready-made summary metrics are deliberately ignored.
    """
    if profile is not None:
        return _strict_raw_experiment(run_dir, relative, title, profile)
    return _legacy_raw_experiment(
        run_dir,
        relative,
        title,
        required_true,
        required_event_types,
        numeric_minimums,
        numeric_maximums,
        minimum_event_counts,
    )


def heatmap_status(run_dir: Path) -> dict[str, Any]:
    required = ("rss", "sinr", "js", "degradation_zone", "service_tier")
    missing = [name for name in required if not (run_dir / "heatmaps" / f"{name}.png").is_file()]
    empty = [
        name
        for name in required
        if (run_dir / "heatmaps" / f"{name}.png").is_file()
        and (run_dir / "heatmaps" / f"{name}.png").stat().st_size == 0
    ]
    summary = load_json(run_dir / "heatmaps" / "heatmap_summary.json")
    failures = []
    if missing:
        failures.append("missing heatmaps: " + ", ".join(missing))
    if empty:
        failures.append("empty heatmaps: " + ", ".join(empty))
    for name in required:
        image_path = run_dir / "heatmaps" / f"{name}.png"
        if image_path.is_file():
            try:
                if image_path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                    failures.append(f"{name}.png is not a PNG file")
            except OSError as exc:
                failures.append(f"could not read {name}.png: {exc}")
    if not summary:
        failures.append("heatmap_summary.json is missing")
    else:
        if summary.get("test_only") is True or summary.get("acceptance_eligible") is False:
            failures.append("heatmaps were generated in test/mock mode")
        provenance = load_json(run_dir / "metrics/provenance.json")
        joint_runtime = load_json(run_dir / "metrics/joint_runtime.json")
        scene_alignment = load_json(run_dir / "metrics/scene_alignment.json")
        if summary.get("run_id") != run_dir.name:
            failures.append("heatmap run_id does not match")
        if summary.get("runtime_id") != joint_runtime.get("runtime_id"):
            failures.append("heatmap runtime_id does not match joint runtime")
        if summary.get("source_hash") != provenance.get("source_hash"):
            failures.append("heatmap source_hash does not match provenance")
        if summary.get("provider_id") != "tcp_jsonl_real_sionna":
            failures.append("heatmap provider is not the accepted real Sionna path")
        if summary.get("scene_hash") != scene_alignment.get("scene_hash"):
            failures.append("heatmap scene hash does not match scene-alignment evidence")
        radio_hash = (provenance.get("config_hashes") or {}).get("network/config/radio_24ghz.yaml")
        if summary.get("radio_config_hash") != radio_hash:
            failures.append("heatmap radio-config hash does not match provenance")
        file_hashes = summary.get("files") if isinstance(summary.get("files"), dict) else {}
        for name in required:
            if file_hashes.get(f"{name}.png") != sha256_file(
                run_dir / "heatmaps" / f"{name}.png"
            ):
                failures.append(f"heatmap hash mismatch: {name}.png")
    return gate("passed" if not failures else "failed", "heatmap evidence evaluated", {"failures": failures})


def evidence_attestation_status(run_dir: Path) -> dict[str, Any]:
    """Verify the repository-pinned host signature and mandatory external ledger."""

    failures: list[str] = []
    try:
        loaded = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text(encoding="utf-8")
        )
        if not isinstance(loaded, dict):
            raise ValueError("dependency lock root is not a mapping")
        policy = loaded.get("evidence_attestation")
        if not isinstance(policy, dict):
            raise ValueError("evidence_attestation policy is not a mapping")
        if policy.get("required") is not True or policy.get("status") != "complete":
            raise ValueError("external evidence-attestation policy is not complete")
        key_id = policy.get("key_id")
        fingerprint = policy.get("public_key_sha256")
        public_relative = policy.get("public_key_path")
        ledger_value = policy.get("ledger_directory")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("evidence-attestation key_id is invalid")
        if not isinstance(fingerprint, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", fingerprint
        ) is None:
            raise ValueError("evidence-attestation public-key fingerprint is invalid")
        if not isinstance(public_relative, str) or not public_relative or "\x00" in public_relative:
            raise ValueError("evidence-attestation public-key path is invalid")
        public_path = (ROOT_DIR / public_relative).resolve(strict=True)
        public_path.relative_to(ROOT_DIR)
        if not isinstance(ledger_value, str) or not Path(ledger_value).is_absolute():
            raise ValueError("evidence-attestation ledger path is not absolute")
        ledger_path = Path(ledger_value)

        from network.validation.evidence_attestation import (
            TrustedPublicKey,
            verify_evidence_attestation,
        )

        return verify_evidence_attestation(
            run_dir,
            {key_id: TrustedPublicKey(public_path, fingerprint)},
            ledger_dir=ledger_path,
        )
    except Exception as exc:
        failures.append(str(exc) or exc.__class__.__name__)
    return gate(
        "failed",
        "external evidence attestation is missing, incomplete, or invalid",
        {"failures": failures},
    )


def ns3_build_receipt_evidence_status(run_dir: Path) -> dict[str, Any]:
    """Independently inspect the fixed accepted TapBridge build receipt."""

    path = run_dir / "metrics/ns3_tap_build_receipt.json"
    failures: list[str] = []
    data = load_json(path)
    if not data:
        return gate("failed", "ns-3 TapBridge build receipt is missing or invalid")
    expected_fields = {
        "schema_version",
        "contract",
        "created_utc",
        "subject_sha256",
        "subject",
    }
    if set(data) != expected_fields:
        failures.append("ns-3 receipt fields differ from the v1 contract")
    if data.get("schema_version") != 1 or data.get("contract") != "ams.ns3.build-receipt/v1":
        failures.append("ns-3 receipt schema/contract is invalid")
    try:
        created = datetime.fromisoformat(str(data.get("created_utc")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("timezone missing")
    except (TypeError, ValueError):
        failures.append("ns-3 receipt created_utc is invalid")
    subject = data.get("subject") if isinstance(data.get("subject"), dict) else {}
    try:
        from network.ns3.ns3_build_receipt import subject_digest

        if data.get("subject_sha256") != subject_digest(subject):
            failures.append("ns-3 receipt subject digest is invalid")
    except Exception as exc:
        failures.append(f"ns-3 receipt subject could not be hashed: {exc}")
    if subject.get("program") != "ams-tap-vertical-slice":
        failures.append("ns-3 receipt is not for the accepted TapBridge program")
    official = subject.get("official_source") if isinstance(subject.get("official_source"), dict) else {}
    if (
        official.get("root") != "/workspace/multiagent_simulation/.external/ns-3"
        or official.get("version") != "3.40"
        or official.get("core_tree_files") != 3764
        or official.get("core_tree_sha256")
        != "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
    ):
        failures.append("ns-3 receipt does not bind the canonical official 3.40 source tree")
    scratch = subject.get("scratch_source") if isinstance(subject.get("scratch_source"), dict) else {}
    project = scratch.get("project") if isinstance(scratch.get("project"), dict) else {}
    copied = scratch.get("copied") if isinstance(scratch.get("copied"), dict) else {}
    current_source = ROOT_DIR / "network/ns3/scratch/ams-tap-vertical-slice.cc"
    current_hash = sha256_file(current_source)
    if (
        project.get("path")
        != "/workspace/multiagent_simulation/network/ns3/scratch/ams-tap-vertical-slice.cc"
        or copied.get("path")
        != "/workspace/multiagent_simulation/.external/ns-3/scratch/ams-tap-vertical-slice.cc"
        or project.get("sha256") != current_hash
        or copied.get("sha256") != current_hash
        or scratch.get("byte_identical") is not True
    ):
        failures.append("ns-3 receipt scratch source does not match current accepted source")
    build = subject.get("build") if isinstance(subject.get("build"), dict) else {}
    try:
        lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text(encoding="utf-8")
        )
        if not isinstance(lock, dict):
            raise ValueError("dependency lock root is not a mapping")
        dependencies = lock.get("dependencies")
        ns3_lock = dependencies.get("ns3") if isinstance(dependencies, dict) else None
        if not isinstance(ns3_lock, dict) or not isinstance(
            ns3_lock.get("required_modules"), list
        ):
            raise ValueError("dependency lock ns3.required_modules is invalid")
        expected_modules = sorted(ns3_lock["required_modules"])
    except Exception as exc:
        failures.append(f"ns-3 dependency-lock modules could not be read: {exc}")
        expected_modules = []
    if build.get("enabled_modules") != expected_modules or build.get("required_modules") != expected_modules:
        failures.append("ns-3 receipt enabled modules differ from dependency lock")
    executable = subject.get("executable") if isinstance(subject.get("executable"), dict) else {}
    if (
        executable.get("path")
        != "/workspace/multiagent_simulation/.external/ns-3/build/scratch/ns3.40-ams-tap-vertical-slice-default"
        or not isinstance(executable.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", executable.get("sha256", "")) is None
        or not nonnegative_integer(executable.get("size_bytes"))
        or executable.get("size_bytes", 0) < 1
        or not nonnegative_integer(executable.get("mode"))
        or executable.get("mode", 0) & 0o111 == 0
    ):
        failures.append("ns-3 receipt executable identity is invalid")
    return gate(
        "passed" if not failures else "failed",
        "ns-3 TapBridge build receipt evaluated from current source and dependency lock",
        {"failures": failures, "executable_sha256": executable.get("sha256")},
    )


def artifact_status(run_dir: Path, matrix_path: Path | None = None) -> dict[str, Any]:
    matrix_path = (matrix_path or ROOT_DIR / "network/config/validation_matrix.yaml").resolve()
    try:
        authoritative_matrix_path = (ROOT_DIR / "network/config/validation_matrix.yaml").resolve()
        if matrix_path.read_bytes() != authoritative_matrix_path.read_bytes():
            raise ValueError(
                "acceptance matrix is not byte-identical to network/config/validation_matrix.yaml"
            )
        matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
        required = tuple(matrix["run_outputs"]["raw_runtime_required"])
        validator_outputs = set(matrix["run_outputs"]["validator_outputs"])
        manifest_relative = str(matrix["run_outputs"]["raw_seal"])
    except Exception as exc:
        return gate("failed", f"validation matrix is invalid: {exc}")
    overlap = validator_outputs.intersection(required)
    if overlap:
        return gate(
            "failed",
            "validation matrix mixes derived outputs into raw evidence",
            {"overlap": sorted(overlap)},
        )
    failures: list[str] = []
    raw_paths: dict[str, Path] = {}
    raw_stats: dict[str, os.stat_result] = {}
    inode_owner: dict[tuple[int, int], str] = {}
    for relative in required:
        source, source_stat, source_error = sealed_regular_file(run_dir, relative)
        if source_error:
            failures.append(source_error)
        if source is None or source_stat is None:
            continue
        raw_paths[relative] = source
        raw_stats[relative] = source_stat
        inode = (source_stat.st_dev, source_stat.st_ino)
        previous = inode_owner.get(inode)
        if previous is not None:
            failures.append(
                f"sealed raw evidence paths share one inode: {previous}, {relative}"
            )
        else:
            inode_owner[inode] = relative
        if source_stat.st_size == 0:
            failures.append(f"sealed raw evidence is empty: {relative}")

    manifest_path, manifest_stat, manifest_path_error = sealed_regular_file(
        run_dir, manifest_relative
    )
    if manifest_path_error:
        failures.append(manifest_path_error)
    manifest = load_json(manifest_path) if manifest_path is not None else {}
    if not manifest:
        failures.append(f"raw evidence seal is missing or invalid: {manifest_relative}")
    else:
        if manifest_stat is not None and manifest_stat.st_mode & 0o222:
            failures.append("evidence manifest remains writable")
        expected_manifest_keys = {
            "schema_version",
            "run_id",
            "runtime_id",
            "source_hash",
            "sealed_utc",
            "matrix_sha256",
            "files",
        }
        if set(manifest) != expected_manifest_keys:
            failures.append("evidence manifest fields differ from the v2 schema")
        if manifest.get("schema_version") != 2:
            failures.append("evidence manifest schema_version is not 2")
        if manifest.get("run_id") != run_dir.name:
            failures.append("evidence manifest run_id does not match")
        provenance = load_json(run_dir / "metrics/provenance.json")
        joint_runtime = load_json(run_dir / "metrics/joint_runtime.json")
        if manifest.get("source_hash") != provenance.get("source_hash"):
            failures.append("evidence manifest source_hash does not match provenance")
        if manifest.get("runtime_id") != joint_runtime.get("runtime_id"):
            failures.append("evidence manifest runtime_id does not match joint runtime")
        if manifest.get("matrix_sha256") != sha256_file(matrix_path):
            failures.append("evidence manifest validation-matrix hash does not match")
        try:
            sealed_utc = datetime.fromisoformat(str(manifest.get("sealed_utc")).replace("Z", "+00:00"))
            if sealed_utc.tzinfo is None:
                raise ValueError("timezone missing")
        except (TypeError, ValueError):
            failures.append("evidence manifest sealed_utc is invalid")
        files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        if not files:
            failures.append("evidence manifest file map is empty")
        if set(files) != set(required):
            failures.append("evidence manifest file set differs from required raw set")
        for relative in required:
            record = files.get(relative) if isinstance(files.get(relative), dict) else {}
            source = raw_paths.get(relative)
            if set(record) != {"sha256", "size_bytes"}:
                failures.append(f"sealed record fields are invalid: {relative}")
            source_stat = raw_stats.get(relative)
            if source_stat is not None and source_stat.st_mode & 0o222:
                failures.append(f"sealed raw evidence remains writable: {relative}")
            actual_hash = sha256_file(source) if source is not None else None
            actual_size = source_stat.st_size if source_stat is not None else None
            if (
                not isinstance(record.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")) is None
                or not nonnegative_integer(record.get("size_bytes"))
                or record.get("size_bytes", 0) < 1
                or record.get("sha256") != actual_hash
                or record.get("size_bytes") != actual_size
            ):
                failures.append(f"sealed raw evidence changed or is unsealed: {relative}")
    attestation = evidence_attestation_status(run_dir)
    if attestation.get("status") != "passed":
        failures.append("external Ed25519 evidence attestation did not pass")
    ns3_receipt = ns3_build_receipt_evidence_status(run_dir)
    if ns3_receipt.get("status") != "passed":
        failures.append("accepted ns-3 TapBridge build receipt did not pass")
    return gate(
        "passed" if not failures else "failed",
        "sealed raw artifact structure and external attestation evaluated; derived validator outputs are excluded",
        {"failures": failures, "attestation": attestation, "ns3_build_receipt": ns3_receipt},
    )


def hitl_status(run_dir: Path, mode: str) -> dict[str, Any]:
    summary = load_json(run_dir / "metrics" / "hitl_loopback_summary.json")
    if not summary:
        return gate("not_run", "HitL loopback summary is missing")
    record = summary.get(mode) if isinstance(summary.get(mode), dict) else {}
    failures = []
    if record.get("passed") is not True:
        failures.append(f"{mode} loopback did not pass")
    if summary.get("actual_modeled_path_available") is not True:
        failures.append("actual modeled path is not available")
    log_path = run_dir / "logs" / "hitl_loopback.jsonl"
    actual_ns3 = False
    actual_provider = False
    false_markers = False
    if log_path.is_file():
        for line in log_path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            actual_ns3 = actual_ns3 or event.get("actual_ns3") is True
            actual_provider = actual_provider or event.get("actual_provider") is True
            false_markers = false_markers or event.get("actual_ns3") is False or event.get("actual_provider") is False
    if not actual_ns3 or not actual_provider or false_markers:
        failures.append("HitL events do not prove actual ns-3 and actual provider traversal")
    return gate("passed" if not failures else "failed", f"{mode} HitL evidence evaluated", {"failures": failures})


def long_run_status(run_dir: Path, minimum_s: float = 1800.0) -> dict[str, Any]:
    summary = load_json(run_dir / "metrics" / "runtime_summary.json")
    duration = summary.get("duration_s")
    timing = run_dir / "logs" / "timing.jsonl"
    queues = run_dir / "metrics" / "queues.csv"
    failures = []
    if not finite_number(duration) or float(duration) < minimum_s:
        failures.append(f"duration {duration!r} is below {minimum_s}s")
    if not timing.is_file() or timing.stat().st_size == 0:
        failures.append("timing log is missing")
    if not queues.is_file() or queues.stat().st_size == 0:
        failures.append("queue metrics are missing")
    final_depths: dict[str, float] = {}
    if queues.is_file():
        try:
            with queues.open(newline="", errors="replace") as handle:
                for row in csv.DictReader(handle):
                    try:
                        depth = float(row.get("queue_depth_packets") or 0)
                        if not math.isfinite(depth) or depth < 0:
                            failures.append("queue depth contains non-finite or negative value")
                            continue
                        final_depths[str(row.get("traffic_class"))] = depth
                    except ValueError:
                        continue
        except OSError:
            failures.append("queue metrics could not be read")
    if any(depth > 1000 for depth in final_depths.values()):
        failures.append("final queue depth exceeds 1000 packets")
    return gate("passed" if not failures else "not_run", "long-run evidence evaluated", {"failures": failures})


def evaluate_run(
    run_dir: Path,
    long_run_minimum_s: float = 1800.0,
    matrix_path: Path | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    runtime_summary = load_json(run_dir / "metrics" / "runtime_summary.json")
    if not runtime_summary:
        runtime_summary = load_json(run_dir / "metrics" / "summary.json")
    packet_provenance = load_json(run_dir / "metrics" / "packet_provenance.json")
    nonce = packet_provenance.get("run_nonce") if isinstance(packet_provenance.get("run_nonce"), str) else None
    pcaps = inspect_class_pcaps(run_dir, nonce=nonce)
    delivery = delivery_status(runtime_summary)

    p0 = {
        "dependency_check": dependency_status(run_dir),
        "provenance": provenance_status(run_dir),
        "joint_runtime": joint_runtime_status(run_dir),
        "five_uav_health": five_uav_health_status(run_dir),
        "packet_provenance": packet_provenance_status(run_dir, pcaps, delivery),
        "no_bypass": no_bypass_status(run_dir),
        "three_traffic_classes": gate(
            "passed" if pcaps["passed"] and delivery["passed"] else "failed",
            "traffic-class PCAP content and delivery counters evaluated",
            {"pcap": pcaps, "delivery": delivery},
        ),
        "online_sionna": online_sionna_status(run_dir),
        "sionna_causality": _raw_experiment(
            run_dir,
            "metrics/sionna_causality.json",
            "Sionna causality",
            ("real_provider", "node_query_apply_packet_correlation", "packet_outcome_changed"),
            (
                "node_state",
                "sionna_query",
                "link_state_applied",
                "packet_outcome_baseline",
                "packet_outcome_impaired",
            ),
            numeric_minimums={"correlated_packets": 1},
            profile="sionna_causality",
        ),
        "link_locality": _raw_experiment(
            run_dir,
            "metrics/link_locality.json",
            "link-local impairment",
            ("target_link_changed", "control_link_within_tolerance"),
            (
                "target_link_baseline",
                "target_link_impaired",
                "control_link_baseline",
                "control_link_impaired",
            ),
            profile="link_locality",
        ),
        "shared_medium": _raw_experiment(
            run_dir,
            "metrics/contention_experiment.json",
            "shared-medium contention",
            ("single_flow_baseline", "concurrent_flow_run", "measurable_effect", "queues_bounded"),
            ("single_flow_sample", "concurrent_flow_sample", "queue_sample"),
            profile="shared_medium",
        ),
        "priority": _raw_experiment(
            run_dir,
            "metrics/priority_experiment.json",
            "control priority under overload",
            ("offered_load_at_least_2x_capacity", "payload_degraded_before_control", "ns3_owned_priority"),
            ("overload_offer", "control_delivery", "payload_delivery", "queue_sample"),
            numeric_maximums={"control_p95_ms": 250.0, "control_loss_rate": 0.05},
            profile="priority",
        ),
        "jamming": _raw_experiment(
            run_dir,
            "metrics/jammer_experiment.json",
            "jammer off/on/off",
            ("off_on_off_completed", "sinr_changed", "js_changed", "packet_outcome_changed", "recovered"),
            ("jammer_off_before", "jammer_on", "jammer_off_after", "packet_outcome"),
            minimum_event_counts={"packet_outcome": 3},
            profile="jamming",
        ),
        "time_coherence": _raw_experiment(
            run_dir,
            "metrics/time_coherence.json",
            "time coherence",
            ("asynchronous_sionna", "state_expiry_enforced", "zero_stale_pose"),
            ("sionna_update", "packet_decision", "pose_sample", "realtime_sample"),
            numeric_minimums={"realtime_factor_min": 0.95},
            numeric_maximums={"realtime_factor_max": 1.05, "late_update_ratio": 0.05},
            profile="time_coherence",
        ),
        "scene_alignment": _raw_experiment(
            run_dir,
            "metrics/scene_alignment.json",
            "scene/frame alignment",
            ("shared_scene_hash", "frame_contract_recorded", "landmarks_aligned"),
            ("scene_hash", "frame_contract", "landmark_measurement"),
            numeric_maximums={"max_landmark_error_m": 1.0},
            profile="scene_alignment",
        ),
        "heatmaps": heatmap_status(run_dir),
        "artifacts": artifact_status(run_dir, matrix_path=matrix_path),
        "repeatability": _raw_experiment(
            run_dir,
            "metrics/repeatability.json",
            "clean-clone repeatability",
            ("dependencies_pinned", "both_runs_passed"),
            ("clean_clone_run",),
            numeric_minimums={"clean_clone_runs": 2},
            minimum_event_counts={"clean_clone_run": 2},
            profile="repeatability",
        ),
    }

    p1 = {
        "hitl_serial": hitl_status(run_dir, "serial"),
        "hitl_ethernet": hitl_status(run_dir, "ethernet"),
        "long_run": long_run_status(run_dir, minimum_s=long_run_minimum_s),
        "customer_map": _raw_experiment(
            run_dir,
            "metrics/customer_map.json",
            "customer map",
            ("converted", "loaded_by_sionna", "scene_hash_recorded"),
            ("map_conversion", "sionna_scene_load", "scene_hash"),
        ),
    }
    p2 = {
        "web_ui": gate("not_run", "optional P2 feature"),
        "advanced_cyber_models": gate("not_run", "optional P2 feature"),
        "alternate_simulator": gate("not_run", "optional P2 feature"),
    }

    p0_passed = all(item["status"] == "passed" for item in p0.values())
    return {
        "schema_version": 2,
        "validation_engine": "network.validation.evidence",
        "run_id": run_dir.name,
        "p0_passed": p0_passed,
        "customer_ready": p0_passed,
        "gates": {"p0": p0, "p1": p1, "p2": p2},
        "runtime_metrics": runtime_summary,
    }
