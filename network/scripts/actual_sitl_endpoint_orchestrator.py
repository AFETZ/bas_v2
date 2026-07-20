#!/usr/bin/env python3
"""Build and supervise the five-UAV actual-SITL endpoint authorization contract.

Normal mode independently verifies five adapter candidates, grants immutable
per-UAV authorizations, waits for five ready receipts, and continuously fails
closed if any adapter, MAVProxy UDP peer, or MAVProxy-to-ArduCopter TCP lineage
is absent or replaced.  ``--build-manifest`` snapshots exact live process and
namespace identities for a runner without duplicating endpoint topology rules.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.bridge.actual_sitl_mavlink_endpoint import (  # noqa: E402
    AUTHORIZATION_CONTRACT,
    EXPECTED_PROCESS_KEYS,
    EXPECTED_UAVS,
    MANIFEST_CONTRACT,
    PROCESS_IDENTITY_KEYS,
    READY_CONTRACT,
    EndpointError,
    JsonlAudit,
    LineageError,
    _adapter_paths,
    _assert_process_roles,
    _flag_values,
    _validate_adapter_candidate_base,
    channel_by_uav,
    document_sha256,
    expected_process_identity,
    publish_json_exclusive,
    read_process_identity,
    sha256_file,
    strict_json,
    utc_now,
    validate_manifest,
    verify_adapter_sockets,
    verify_channel_lineage,
)
from network.bridge.runtime_clock_beacon import beacon  # noqa: E402


AGGREGATE_READY_CONTRACT = "ams.actual-sitl-endpoints-ready/v1"
SUPERVISOR_FAILURE_CONTRACT = "ams.actual-sitl-endpoint-supervisor-failure/v1"
ADAPTER_SOURCE = ROOT / "network" / "bridge" / "actual_sitl_mavlink_endpoint.py"
RELAY_CORE_SOURCE = ROOT / "network" / "bridge" / "opaque_udp_relay.py"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def process_reference(value: str) -> tuple[str, int, int | None]:
    uav, separator, identity = value.partition("=")
    if not separator or uav not in EXPECTED_UAVS:
        raise argparse.ArgumentTypeError("process reference must be uavN=PID[:START_TICKS]")
    pid_text, tick_separator, ticks_text = identity.partition(":")
    try:
        pid = int(pid_text)
        ticks = int(ticks_text) if tick_separator else None
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid process reference: {value!r}") from exc
    if pid <= 1 or (ticks is not None and ticks <= 0):
        raise argparse.ArgumentTypeError(f"invalid process reference: {value!r}")
    return uav, pid, ticks


def _reference_map(
    values: list[tuple[str, int, int | None]], role: str
) -> dict[str, tuple[int, int | None]]:
    result: dict[str, tuple[int, int | None]] = {}
    for uav, pid, ticks in values:
        if uav in result:
            raise EndpointError(f"duplicate {role} reference for {uav}")
        result[uav] = (pid, ticks)
    if set(result) != set(EXPECTED_UAVS):
        raise EndpointError(
            f"{role} references must name exactly {list(EXPECTED_UAVS)}, got {sorted(result)}"
        )
    return result


def _namespace_inode(namespace: str) -> int:
    path = Path("/var/run/netns") / namespace
    try:
        info = path.stat()
    except OSError as exc:
        raise EndpointError(f"cannot inspect namespace {namespace}: {exc}") from exc
    if not path.is_file() or path.is_symlink() or info.st_ino <= 0:
        raise EndpointError(f"namespace handle is not a non-symlink nsfs file: {path}")
    return int(info.st_ino)


def _snapshot_reference(
    uav: str,
    role: str,
    reference: tuple[int, int | None],
    launch_pgid: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pid, expected_ticks = reference
    identity = read_process_identity(pid)
    if expected_ticks is not None and identity["start_ticks"] != expected_ticks:
        raise EndpointError(
            f"{uav} {role} PID {pid} start_ticks mismatch: "
            f"expected {expected_ticks}, observed {identity['start_ticks']}"
        )
    if identity["pgid"] != launch_pgid:
        raise EndpointError(
            f"{uav} {role} PID {pid} PGID {identity['pgid']} differs from {launch_pgid}"
        )
    return expected_process_identity(identity), identity


def build_manifest(args: argparse.Namespace) -> int:
    if args.run_dir.is_symlink():
        raise EndpointError("run directory symlinks are forbidden")
    run_dir = args.run_dir.resolve(strict=True)
    if not run_dir.is_dir():
        raise EndpointError("run directory must exist")
    required_ids = {
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
    }
    if any(not isinstance(value, str) or not value for value in required_ids.values()):
        raise EndpointError("--run-id, --runtime-id, and --run-nonce are required in build mode")
    if args.launch_pgid is None or args.launch_pgid <= 1:
        raise EndpointError("--launch-pgid is required in build mode")
    mavproxy_refs = _reference_map(args.mavproxy_ref, "MAVProxy")
    sitl_refs = _reference_map(args.sitl_ref, "SITL")
    channels: list[dict[str, Any]] = []
    all_pids: set[int] = set()
    for index, uav in enumerate(EXPECTED_UAVS, start=1):
        mavproxy_expected, mavproxy_live = _snapshot_reference(
            uav, "MAVProxy", mavproxy_refs[uav], args.launch_pgid
        )
        sitl_expected, sitl_live = _snapshot_reference(
            uav, "SITL", sitl_refs[uav], args.launch_pgid
        )
        if mavproxy_expected["pid"] in all_pids or sitl_expected["pid"] in all_pids:
            raise EndpointError("one PID cannot own two actual-SITL roles")
        all_pids.update({mavproxy_expected["pid"], sitl_expected["pid"]})
        channel = {
            "uav": uav,
            "instance": index - 1,
            "system_id": index,
            "namespace": f"ams-uav{index}",
            "namespace_inode": _namespace_inode(f"ams-uav{index}"),
            "radio_bind": {"host": f"10.71.{index}.10", "port": 14600 + index},
            "gcs_peer": {"host": "10.71.0.10", "port": 14600},
            "tail_bind": {"host": f"10.72.{index}.2", "port": 14559 + index},
            "tail_peer_host": f"10.72.{index}.1",
            "tail_pcap_roles": {
                "root": f"tail-root-uav{index}",
                "uav": f"tail-uav{index}",
            },
            "master": {"host": "127.0.0.1", "port": 5760 + 10 * (index - 1)},
            "launch_pgid": args.launch_pgid,
            "mavproxy": mavproxy_expected,
            "sitl": sitl_expected,
        }
        _assert_process_roles(channel, mavproxy_live, sitl_live)
        channels.append(channel)
    manifest = {
        "schema_version": 1,
        "contract": MANIFEST_CONTRACT,
        **required_ids,
        "adapter_source_sha256": sha256_file(ADAPTER_SOURCE),
        "relay_core_source_sha256": sha256_file(RELAY_CORE_SOURCE),
        "peer_lease_ms": args.peer_lease_ms,
        "lineage_check_ms": args.lineage_check_ms,
        "authorization_timeout_ms": args.authorization_timeout_ms,
        "channels": channels,
    }
    validate_manifest(manifest)
    output = args.manifest
    if output.is_symlink():
        raise EndpointError("manifest output symlinks are forbidden")
    output = output.absolute()
    try:
        output.relative_to(run_dir)
    except ValueError as exc:
        raise EndpointError("manifest output must be inside the run directory") from exc
    publish_json_exclusive(output, manifest)
    print(
        f"PASS actual-SITL manifest {output} sha256={document_sha256(manifest)} "
        f"channels={len(channels)}"
    )
    return 0


def _stable_identity_equal(expected: dict[str, Any], actual: dict[str, Any], context: str) -> None:
    expected_subset = expected_process_identity(expected)
    differences = {
        key: {"expected": expected_subset[key], "actual": actual[key]}
        for key in sorted(EXPECTED_PROCESS_KEYS)
        if expected_subset[key] != actual[key]
    }
    if differences:
        raise LineageError(f"{context} process identity differs: {differences}")


def _resolve_process_script(pid: int, argv: list[str]) -> Path:
    try:
        cwd = Path(os.readlink(Path("/proc") / str(pid) / "cwd"))
    except OSError as exc:
        raise LineageError(f"cannot inspect adapter PID {pid} cwd: {exc}") from exc
    matches: list[Path] = []
    for token in argv[:4]:
        if Path(token).name != ADAPTER_SOURCE.name:
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            matches.append(candidate.resolve(strict=True))
        except OSError:
            continue
    if matches != [ADAPTER_SOURCE.resolve()]:
        raise LineageError(f"adapter PID {pid} does not execute the canonical adapter source")
    return matches[0]


def _exact_adapter_argv(
    identity: dict[str, Any], run_dir: Path, manifest_path: Path, uav: str
) -> None:
    _resolve_process_script(identity["pid"], identity["argv"])
    expected_flags = {
        "--run-dir": str(run_dir),
        "--manifest": str(manifest_path),
        "--uav": uav,
    }
    for flag, expected in expected_flags.items():
        values = _flag_values(identity["argv"], flag)
        if flag in {"--run-dir", "--manifest"} and len(values) == 1:
            try:
                value = str(Path(values[0]).resolve(strict=True))
            except OSError as exc:
                raise LineageError(f"adapter {flag} path is invalid: {exc}") from exc
        else:
            value = values[0] if len(values) == 1 else None
        if value != expected:
            raise LineageError(f"adapter must have exactly one {flag} {expected!r}")


def verify_candidate_external(
    candidate: dict[str, Any],
    manifest: dict[str, Any],
    manifest_hash: str,
    channel: dict[str, Any],
    run_dir: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    _validate_adapter_candidate_base(candidate, manifest, manifest_hash, channel)
    adapter = candidate["adapter"]
    if not isinstance(adapter, dict) or set(adapter) != PROCESS_IDENTITY_KEYS:
        raise EndpointError(f"{channel['uav']} candidate adapter identity is incomplete")
    actual_adapter = verify_adapter_sockets(
        int(adapter["pid"]),
        channel,
        int(candidate["radio_socket"].get("inode", -1)),
        int(candidate["tail_socket"].get("inode", -1)),
    )
    _stable_identity_equal(adapter, actual_adapter["identity"], f"{channel['uav']} adapter")
    _exact_adapter_argv(actual_adapter["identity"], run_dir, manifest_path, channel["uav"])
    if sha256_file(ADAPTER_SOURCE) != manifest["adapter_source_sha256"]:
        raise LineageError("canonical adapter source changed after manifest publication")
    if sha256_file(RELAY_CORE_SOURCE) != manifest["relay_core_source_sha256"]:
        raise LineageError("shared byte-opaque relay core changed after manifest publication")
    if candidate["radio_socket"] != actual_adapter["radio_socket"]:
        raise LineageError(f"{channel['uav']} candidate radio socket was replaced")
    if candidate["tail_socket"] != actual_adapter["tail_socket"]:
        raise LineageError(f"{channel['uav']} candidate tail socket was replaced")
    peer = candidate["mavproxy_peer"]
    if not isinstance(peer, dict) or set(peer) != {"host", "port"}:
        raise EndpointError(f"{channel['uav']} candidate MAVProxy peer is malformed")
    peer_tuple = (str(peer["host"]), int(peer["port"]))
    lineage = verify_channel_lineage(channel, peer_tuple, hash_executable=True)
    declared_lineage = candidate["lineage"]
    if not isinstance(declared_lineage, dict):
        raise EndpointError(f"{channel['uav']} candidate lineage is malformed")
    for process_role in ("mavproxy", "sitl"):
        declared_process = declared_lineage.get(process_role)
        if not isinstance(declared_process, dict) or set(declared_process) != PROCESS_IDENTITY_KEYS:
            raise EndpointError(f"{channel['uav']} candidate {process_role} identity is incomplete")
        _stable_identity_equal(
            declared_process, lineage[process_role], f"{channel['uav']} candidate {process_role}"
        )
    for socket_role in (
        "mavproxy_udp_peer",
        "mavproxy_udp_socket",
        "mavproxy_tcp_master",
        "sitl_tcp_master",
    ):
        if declared_lineage.get(socket_role) != lineage[socket_role]:
            raise LineageError(f"{channel['uav']} candidate {socket_role} differs from live lineage")
    first = candidate["first_tail_datagram"]
    if (
        not isinstance(first, dict)
        or set(first) != {"bytes", "sha256", "mavlink_source_system_ids"}
        or isinstance(first["bytes"], bool)
        or not isinstance(first["bytes"], int)
        or first["bytes"] <= 0
        or not isinstance(first["sha256"], str)
        or HEX64.fullmatch(first["sha256"]) is None
        or not isinstance(first["mavlink_source_system_ids"], list)
        or channel["system_id"] not in first["mavlink_source_system_ids"]
    ):
        raise EndpointError(f"{channel['uav']} candidate lacks a real vehicle MAVLink datagram")
    return {
        "adapter": actual_adapter,
        "lineage": lineage,
        "peer": peer_tuple,
    }


def issue_authorization(
    manifest: dict[str, Any],
    manifest_hash: str,
    channel: dict[str, Any],
    candidate: dict[str, Any],
    candidate_hash: str,
    path: Path,
) -> dict[str, Any]:
    issuer = read_process_identity(os.getpid())
    authorization = {
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
        "issuer": issuer,
        "authorized_wall_utc": utc_now(),
        "authorized_monotonic_ns": time.monotonic_ns(),
    }
    publish_json_exclusive(path, authorization)
    return authorization


def validate_ready_external(
    ready: dict[str, Any],
    manifest: dict[str, Any],
    manifest_hash: str,
    channel: dict[str, Any],
    candidate: dict[str, Any],
    authorization: dict[str, Any],
    run_dir: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "contract",
        "status",
        "run_id",
        "runtime_id",
        "run_nonce",
        "uav",
        "system_id",
        "manifest_sha256",
        "candidate_sha256",
        "authorization_sha256",
        "ready_wall_utc",
        "ready_monotonic_ns",
        "adapter",
        "radio_socket",
        "tail_socket",
        "mavproxy_peer",
        "lineage",
    }
    if set(ready) != expected_keys:
        raise EndpointError(f"{channel['uav']} ready receipt keys differ")
    expected = {
        "schema_version": 1,
        "contract": READY_CONTRACT,
        "status": "ready",
        "run_id": manifest["run_id"],
        "runtime_id": manifest["runtime_id"],
        "run_nonce": manifest["run_nonce"],
        "uav": channel["uav"],
        "system_id": channel["system_id"],
        "manifest_sha256": manifest_hash,
        "candidate_sha256": document_sha256(candidate),
        "authorization_sha256": document_sha256(authorization),
    }
    if any(ready.get(key) != value for key, value in expected.items()):
        raise EndpointError(f"{channel['uav']} ready receipt identity differs")
    current = verify_candidate_external(
        candidate, manifest, manifest_hash, channel, run_dir, manifest_path
    )
    _stable_identity_equal(ready["adapter"], current["adapter"]["identity"], f"{channel['uav']} ready adapter")
    if ready["radio_socket"] != current["adapter"]["radio_socket"]:
        raise LineageError(f"{channel['uav']} ready radio socket differs")
    if ready["tail_socket"] != current["adapter"]["tail_socket"]:
        raise LineageError(f"{channel['uav']} ready tail socket differs")
    if ready["mavproxy_peer"] != {"host": current["peer"][0], "port": current["peer"][1]}:
        raise LineageError(f"{channel['uav']} ready MAVProxy peer differs")
    return current


def _supervisor_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "log": run_dir / "logs" / "actual_sitl_supervisor.jsonl",
        "failure": run_dir / "raw" / "actual_sitl" / "endpoint-supervisor.failure.json",
    }


def _publish_supervisor_failure(
    path: Path, manifest: dict[str, Any], manifest_hash: str, reason: str
) -> None:
    document = {
        "schema_version": 1,
        "contract": SUPERVISOR_FAILURE_CONTRACT,
        "status": "failed_closed",
        "run_id": manifest["run_id"],
        "runtime_id": manifest["runtime_id"],
        "run_nonce": manifest["run_nonce"],
        "manifest_sha256": manifest_hash,
        "failure_wall_utc": utc_now(),
        "failure_monotonic_ns": time.monotonic_ns(),
        "reason": reason,
    }
    try:
        publish_json_exclusive(path, document)
    except EndpointError:
        pass


def supervise(args: argparse.Namespace) -> int:
    if args.run_dir.is_symlink() or args.manifest.is_symlink():
        raise EndpointError("run directory and manifest symlinks are forbidden")
    run_dir = args.run_dir.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    try:
        manifest_path.relative_to(run_dir)
    except ValueError as exc:
        raise EndpointError("manifest must be inside the run directory") from exc
    manifest = validate_manifest(strict_json(manifest_path))
    if sha256_file(ADAPTER_SOURCE) != manifest["adapter_source_sha256"]:
        raise EndpointError("adapter source differs from the manifest")
    if sha256_file(RELAY_CORE_SOURCE) != manifest["relay_core_source_sha256"]:
        raise EndpointError("shared byte-opaque relay core differs from the manifest")
    manifest_hash = document_sha256(manifest)
    ready_path = (
        args.ready_file.absolute()
        if args.ready_file is not None
        else run_dir / "raw" / "state" / "actual-sitl-endpoints.ready.json"
    )
    stop_path = (
        args.stop_file.absolute()
        if args.stop_file is not None
        else run_dir / "raw" / "state" / "actual-sitl-endpoints.stop"
    )
    for path, label in ((ready_path, "ready file"), (stop_path, "stop file")):
        try:
            path.relative_to(run_dir)
        except ValueError as exc:
            raise EndpointError(f"{label} must be inside the run directory") from exc
    paths = _supervisor_paths(run_dir)
    audit = JsonlAudit(paths["log"], manifest, "all")
    manifest_deadline = time.monotonic_ns() + manifest["authorization_timeout_ms"] * 1_000_000
    stop = False
    clock_stop = threading.Event()
    clock_thread: threading.Thread | None = None

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    candidates: dict[str, dict[str, Any]] = {}
    authorizations: dict[str, dict[str, Any]] = {}
    ready_receipts: dict[str, dict[str, Any]] = {}
    verified: dict[str, dict[str, Any]] = {}
    audit.emit(
        "supervisor_start_not_ready",
        pid=os.getpid(),
        manifest_sha256=manifest_hash,
        expected_uavs=list(EXPECTED_UAVS),
    )
    try:
        if getattr(args, "clock_socket", None) is not None:
            clock_thread = threading.Thread(
                target=beacon,
                args=(
                    args.clock_socket.resolve(),
                    "actual_endpoint_supervisor",
                    clock_stop,
                ),
                name="actual-endpoint-supervisor-clock",
            )
            clock_thread.start()
        while len(authorizations) < 5:
            if stop or stop_path.exists():
                raise EndpointError("supervisor stopped before all five authorizations")
            if time.monotonic_ns() >= manifest_deadline:
                missing = sorted(set(EXPECTED_UAVS) - set(authorizations))
                raise EndpointError(f"candidate authorization timed out; missing={missing}")
            for uav in EXPECTED_UAVS:
                if uav in authorizations:
                    continue
                channel = channel_by_uav(manifest, uav)
                endpoint_paths = _adapter_paths(run_dir, uav)
                if endpoint_paths["failure"].exists():
                    raise LineageError(f"{uav} adapter failed before authorization")
                if not endpoint_paths["candidate"].exists():
                    continue
                candidate = strict_json(endpoint_paths["candidate"])
                state = verify_candidate_external(
                    candidate, manifest, manifest_hash, channel, run_dir, manifest_path
                )
                candidate_hash = document_sha256(candidate)
                authorization = issue_authorization(
                    manifest,
                    manifest_hash,
                    channel,
                    candidate,
                    candidate_hash,
                    endpoint_paths["authorization"],
                )
                candidates[uav] = candidate
                authorizations[uav] = authorization
                verified[uav] = state
                audit.emit(
                    "endpoint_authorized_not_aggregate_ready",
                    endpoint_uav=uav,
                    candidate_sha256=candidate_hash,
                    authorization_sha256=document_sha256(authorization),
                    mavproxy_peer={"host": state["peer"][0], "port": state["peer"][1]},
                )
            time.sleep(args.poll_ms / 1000.0)

        while len(ready_receipts) < 5:
            if stop or stop_path.exists():
                raise EndpointError("supervisor stopped before all five ready receipts")
            if time.monotonic_ns() >= manifest_deadline:
                missing = sorted(set(EXPECTED_UAVS) - set(ready_receipts))
                raise EndpointError(f"adapter readiness timed out; missing={missing}")
            for uav in EXPECTED_UAVS:
                if uav in ready_receipts:
                    continue
                channel = channel_by_uav(manifest, uav)
                endpoint_paths = _adapter_paths(run_dir, uav)
                if endpoint_paths["failure"].exists():
                    raise LineageError(f"{uav} adapter failed before ready")
                if not endpoint_paths["ready"].exists():
                    continue
                ready = strict_json(endpoint_paths["ready"])
                verified[uav] = validate_ready_external(
                    ready,
                    manifest,
                    manifest_hash,
                    channel,
                    candidates[uav],
                    authorizations[uav],
                    run_dir,
                    manifest_path,
                )
                ready_receipts[uav] = ready
                audit.emit(
                    "endpoint_ready_not_aggregate_ready",
                    endpoint_uav=uav,
                    ready_sha256=document_sha256(ready),
                )
            time.sleep(args.poll_ms / 1000.0)

        aggregate = {
            "schema_version": 1,
            "contract": AGGREGATE_READY_CONTRACT,
            "status": "ready",
            "run_id": manifest["run_id"],
            "runtime_id": manifest["runtime_id"],
            "run_nonce": manifest["run_nonce"],
            "manifest_sha256": manifest_hash,
            "ready_wall_utc": utc_now(),
            "ready_monotonic_ns": time.monotonic_ns(),
            "supervisor": read_process_identity(os.getpid()),
            "channels": {
                uav: {
                    "system_id": channel_by_uav(manifest, uav)["system_id"],
                    "candidate_sha256": document_sha256(candidates[uav]),
                    "authorization_sha256": document_sha256(authorizations[uav]),
                    "ready_sha256": document_sha256(ready_receipts[uav]),
                    "radio_socket": ready_receipts[uav]["radio_socket"],
                    "tail_socket": ready_receipts[uav]["tail_socket"],
                    "mavproxy_peer": ready_receipts[uav]["mavproxy_peer"],
                    "tail_pcap_roles": channel_by_uav(manifest, uav)["tail_pcap_roles"],
                }
                for uav in EXPECTED_UAVS
            },
        }
        publish_json_exclusive(ready_path, aggregate)
        audit.emit(
            "aggregate_ready",
            ready_path=str(ready_path.relative_to(run_dir)),
            ready_sha256=document_sha256(aggregate),
        )

        sample_sequence = 0
        while not stop and not stop_path.exists():
            sample_sequence += 1
            sample: dict[str, str] = {}
            for uav in EXPECTED_UAVS:
                endpoint_paths = _adapter_paths(run_dir, uav)
                if endpoint_paths["failure"].exists():
                    failure = strict_json(endpoint_paths["failure"])
                    raise LineageError(f"{uav} adapter failed closed: {failure.get('reason')}")
                channel = channel_by_uav(manifest, uav)
                state = verify_candidate_external(
                    candidates[uav], manifest, manifest_hash, channel, run_dir, manifest_path
                )
                sample[uav] = document_sha256(
                    {
                        "adapter": state["adapter"],
                        "lineage": state["lineage"],
                        "peer": state["peer"],
                    }
                )
            audit.emit(
                "lineage_sample_pass",
                sample_seq=sample_sequence,
                channel_lineage_sha256=sample,
            )
            time.sleep(max(args.poll_ms, manifest["lineage_check_ms"]) / 1000.0)
        audit.emit("supervisor_stop", reason="signal" if stop else "stop_file")
        return 0
    except (EndpointError, OSError) as exc:
        _publish_supervisor_failure(paths["failure"], manifest, manifest_hash, str(exc))
        audit.emit("supervisor_failed_closed", reason=str(exc))
        print(f"FAIL actual-SITL endpoint supervisor: {exc}", file=sys.stderr)
        return 2
    finally:
        clock_stop.set()
        if clock_thread is not None:
            clock_thread.join(timeout=2.0)
            if clock_thread.is_alive():
                print(
                    "FAIL actual-SITL supervisor clock beacon did not stop",
                    file=sys.stderr,
                )
        audit.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-manifest", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--clock-socket", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--runtime-id")
    parser.add_argument("--run-nonce")
    parser.add_argument("--launch-pgid", type=int)
    parser.add_argument(
        "--mavproxy-ref",
        action="append",
        type=process_reference,
        default=[],
        metavar="uavN=PID[:START_TICKS]",
    )
    parser.add_argument(
        "--sitl-ref",
        action="append",
        type=process_reference,
        default=[],
        metavar="uavN=PID[:START_TICKS]",
    )
    parser.add_argument("--peer-lease-ms", type=int, default=5000)
    parser.add_argument("--lineage-check-ms", type=int, default=250)
    parser.add_argument("--authorization-timeout-ms", type=int, default=60000)
    parser.add_argument("--poll-ms", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.build_manifest:
            return build_manifest(args)
        if args.poll_ms < 10 or args.poll_ms > 5000:
            raise EndpointError("--poll-ms must be in 10..5000")
        return supervise(args)
    except (EndpointError, OSError) as exc:
        print(f"FAIL actual-SITL endpoint orchestration startup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
