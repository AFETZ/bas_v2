#!/usr/bin/env python3
"""Non-blocking TCP worker/service for the Sionna asynchronous v1 protocol.

The network loop never performs radio computation.  Queries enter a bounded
``put_nowait`` queue, one dedicated provider thread owns the Sionna backend,
and completions leave through a bounded ``get_nowait`` queue.  Production
construction accepts only ``network.radio_provider.provider`` in
``real_sionna`` mode; deterministic callable injection is isolated behind
explicit ``for_unit_tests`` constructors.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, Union

from network.radio_provider.sionna_async import (
    MESSAGE_TYPES,
    ProtocolIdentity,
    ProtocolLimits,
    ProtocolStateError,
    ProtocolValidationError,
    WireSequenceTracker,
    decode_message,
    encode_message,
    message_sha256,
    validate_message,
)


class AsyncServiceError(RuntimeError):
    """The async provider cannot safely continue or be constructed."""


class StalePoseError(RuntimeError):
    """Compute backend determined that the pose snapshot is stale."""


class SceneMismatchError(RuntimeError):
    """Compute backend determined that the scene identity does not match."""


class DeadlineMissedError(RuntimeError):
    """Compute backend explicitly reported deadline exhaustion."""


class ComputeBackend(Protocol):
    provider_mode: str
    acceptance_eligible: bool

    def compute(self, query: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the exact ``physical`` object required by protocol v1."""


@dataclass(frozen=True)
class SubmitDecision:
    accepted: bool
    reason: str
    query_id: str


@dataclass(frozen=True)
class ComputeCompletion:
    connection_id: str
    query: Mapping[str, Any]
    raw_query_sha256: str
    status: str
    provider_received_monotonic_ns: int
    provider_started_monotonic_ns: int
    provider_completed_monotonic_ns: int
    physical: Optional[Mapping[str, Any]] = None
    detail: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class WorkerFault:
    reason: str
    fault_monotonic_ns: int


PollItem = Union[ComputeCompletion, WorkerFault]


@dataclass(frozen=True)
class _WorkItem:
    connection_id: str
    query: Mapping[str, Any]
    raw_query_sha256: str
    received_monotonic_ns: int


class _InjectedTestBackend:
    provider_mode = "deterministic_test_injection"
    acceptance_eligible = False

    def __init__(self, compute: Callable[[Mapping[str, Any]], Mapping[str, Any]]):
        self._compute = compute

    def compute(self, query: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._compute(query)


class RealSionnaBackend:
    """Strict adapter around ``provider.SionnaRadioProvider(real_sionna)``."""

    provider_mode = "real_sionna"
    acceptance_eligible = True

    def __init__(self, provider_instance: Any):
        settings = getattr(provider_instance, "settings", None)
        if settings is None or getattr(settings, "mode", None) != "real_sionna":
            raise AsyncServiceError(
                "production backend requires provider mode=real_sionna"
            )
        if not bool(getattr(provider_instance, "acceptance_eligible", False)):
            raise AsyncServiceError(
                "production Sionna provider is not acceptance eligible"
            )
        self._provider = provider_instance

    def warm_up(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Compile the owned real-Sionna path before formal wire traffic."""

        if self.provider_mode != "real_sionna" or not self.acceptance_eligible:
            raise AsyncServiceError("real Sionna warm-up requires an eligible backend")
        response = self._provider.query(copy.deepcopy(dict(request)))
        if not isinstance(response, Mapping) or response.get("type") != "link_state":
            raise AsyncServiceError("real Sionna warm-up returned an invalid response")
        links = response.get("links")
        if (
            not isinstance(links, list)
            or not links
            or any(not isinstance(link, Mapping) or link.get("stale") is not False for link in links)
        ):
            raise AsyncServiceError("real Sionna warm-up did not produce fresh links")
        return response

    def compute(self, query: Mapping[str, Any]) -> Mapping[str, Any]:
        radio = query["radio_assumptions"]
        request = {
            "type": "link_query",
            "time_s": time.time(),
            "deadline_ms": max(
                0.001,
                (
                    int(query["deadline_monotonic_ns"])
                    - int(query["request_sent_monotonic_ns"])
                )
                / 1_000_000.0,
            ),
            "radio": {
                "carrier_hz": float(radio["carrier_frequency_hz"]),
                "bandwidth_hz": float(radio["bandwidth_hz"]),
                "tx_power_dbm": float(radio["tx_power_dbm"]),
                "receiver_noise_figure_db": float(radio["receiver_noise_figure_db"]),
                "receiver_sensitivity_dbm": float(radio["receiver_sensitivity_dbm"]),
            },
            "nodes": [
                {
                    "id": node["node_id"],
                    "role": node["role"],
                    "position_m": list(node["position_m"]),
                    "orientation_quat_xyzw": list(node["orientation_quat_xyzw"]),
                    "antenna": query["antenna_assumptions"]["tx_pattern"],
                }
                for node in query["nodes"]
            ],
            "emitters": [
                {
                    "id": jammer["jammer_id"],
                    "position_m": list(jammer["position_m"]),
                    "center_hz": float(jammer["center_frequency_hz"]),
                    "bandwidth_hz": float(jammer["bandwidth_hz"]),
                    "power_dbm": float(jammer["power_dbm"]),
                    "duty_cycle": float(jammer["duty_cycle"]),
                    "antenna": jammer["antenna_pattern"],
                }
                for jammer in query["jammers"]
                if jammer["enabled"]
            ],
            "links": [
                {
                    "tx": query["tx_node_id"],
                    "rx": query["rx_node_id"],
                    "traffic_class": query["traffic_class"],
                }
            ],
        }
        response = self._provider.query(request)
        links = response.get("links") if isinstance(response, dict) else None
        if not isinstance(links, list) or len(links) != 1:
            raise AsyncServiceError(
                "real Sionna provider returned no unique directed link"
            )
        link = links[0]
        if bool(link.get("stale", False)):
            raise DeadlineMissedError(
                "real Sionna computation exceeded request deadline"
            )
        signal_dbm = float(link["rssi_dbm"])
        js_db = float(link["js_db"])
        interference_dbm = signal_dbm + js_db
        bandwidth_hz = float(radio["bandwidth_hz"])
        noise_figure_db = float(radio["receiver_noise_figure_db"])
        noise_dbm = -174.0 + 10.0 * math.log10(max(bandwidth_hz, 1.0)) + noise_figure_db
        geometry_state = str(link.get("geometry_state", ""))
        if geometry_state not in {"los", "nlos", "blocked_no_path"}:
            raise AsyncServiceError(
                "real Sionna provider returned an unclassified geometry state"
            )
        path_count = link.get("path_count")
        path_type_counts = link.get("path_type_counts")
        expected_path_types = {
            "los",
            "specular",
            "diffuse",
            "refracted",
            "diffracted",
            "mixed",
        }
        if (
            isinstance(path_count, bool)
            or not isinstance(path_count, int)
            or path_count < 0
            or not isinstance(path_type_counts, dict)
            or set(path_type_counts) != expected_path_types
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in path_type_counts.values()
            )
            or sum(path_type_counts.values()) != path_count
        ):
            raise AsyncServiceError(
                "real Sionna provider returned invalid path-count evidence"
            )
        if (path_count == 0) != (geometry_state == "blocked_no_path"):
            raise AsyncServiceError(
                "real Sionna geometry state contradicts its resolved path count"
            )
        if geometry_state == "los" and path_type_counts["los"] == 0:
            raise AsyncServiceError(
                "real Sionna LOS state has no resolved LOS path"
            )
        if geometry_state == "nlos" and path_type_counts["los"] != 0:
            raise AsyncServiceError(
                "real Sionna NLOS state includes a resolved LOS path"
            )
        propagation_delay_ns = float(link["propagation_delay_ns"])
        if not math.isfinite(propagation_delay_ns) or propagation_delay_ns < 0.0:
            raise AsyncServiceError(
                "real Sionna provider returned invalid propagation delay"
            )
        return {
            "pathloss_db": float(link["pathloss_db"]),
            "propagation_delay_ns": propagation_delay_ns,
            "rssi_dbm": signal_dbm,
            "signal_power_dbm": signal_dbm,
            "interference_power_dbm": interference_dbm,
            "noise_power_dbm": noise_dbm,
            "sinr_db": float(link["sinr_db"]),
            "js_db": js_db,
            "geometry_state": geometry_state,
            "path_count": path_count,
            "path_type_counts": dict(path_type_counts),
            "units": {
                "pathloss": "dB",
                "propagation_delay": "ns",
                "rssi": "dBm",
                "signal_power": "dBm",
                "interference_power": "dBm",
                "noise_power": "dBm",
                "sinr": "dB",
                "j_over_s": "dB",
            },
        }


class AsyncSionnaWorker:
    """One-owner compute worker with non-blocking bounded ingress/egress."""

    def __init__(
        self,
        backend: ComputeBackend,
        identity: ProtocolIdentity,
        scene_material_manifest_sha256: str,
        *,
        limits: Optional[ProtocolLimits] = None,
        clock_domain: str = "host-monotonic",
        clock_ns: Callable[[], int] = time.monotonic_ns,
        _unit_test_injection: bool = False,
    ) -> None:
        self.backend = backend
        self.identity = identity
        self.scene_material_manifest_sha256 = scene_material_manifest_sha256
        self.limits = limits or ProtocolLimits()
        self.clock_domain = clock_domain
        self._clock_ns = clock_ns
        self._test_only = _unit_test_injection
        if not _unit_test_injection and (
            backend.provider_mode != "real_sionna" or not backend.acceptance_eligible
        ):
            raise AsyncServiceError(
                "production worker accepts only acceptance-eligible real_sionna backend"
            )
        if _unit_test_injection and not isinstance(backend, _InjectedTestBackend):
            raise AsyncServiceError(
                "unit-test worker requires deterministic injected backend"
            )
        if (
            not isinstance(scene_material_manifest_sha256, str)
            or len(scene_material_manifest_sha256) != 64
            or any(
                ch not in "0123456789abcdef" for ch in scene_material_manifest_sha256
            )
        ):
            raise AsyncServiceError(
                "scene material manifest must be a lowercase SHA-256"
            )
        self._requests: queue.Queue[_WorkItem] = queue.Queue(
            maxsize=self.limits.request_queue_capacity
        )
        self._completions: queue.Queue[ComputeCompletion] = queue.Queue(
            maxsize=self.limits.completion_queue_capacity
        )
        self._stop = threading.Event()
        self._accepting = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._seen_query_ids: set[str] = set()
        self._fatal_reason: Optional[str] = None
        self._fatal_reported = False

    @classmethod
    def for_unit_tests(
        cls,
        compute: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        identity: ProtocolIdentity,
        scene_material_manifest_sha256: str,
        *,
        limits: Optional[ProtocolLimits] = None,
        clock_domain: str = "host-monotonic",
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> "AsyncSionnaWorker":
        return cls(
            _InjectedTestBackend(compute),
            identity,
            scene_material_manifest_sha256,
            limits=limits,
            clock_domain=clock_domain,
            clock_ns=clock_ns,
            _unit_test_injection=True,
        )

    @property
    def test_only(self) -> bool:
        return self._test_only

    @property
    def request_queue_size(self) -> int:
        return self._requests.qsize()

    @property
    def completion_queue_size(self) -> int:
        return self._completions.qsize()

    @property
    def fatal_reason(self) -> Optional[str]:
        with self._state_lock:
            return self._fatal_reason

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                return
            self._accepting.set()
            self._thread = threading.Thread(
                target=self._run,
                name="sionna-real-compute-worker"
                if not self.test_only
                else "sionna-test-compute-worker",
                daemon=True,
            )
            self._thread.start()

    def submit(
        self,
        query_message: Mapping[str, Any],
        *,
        connection_id: str,
        received_monotonic_ns: Optional[int] = None,
        raw_query_sha256: Optional[str] = None,
    ) -> SubmitDecision:
        """Submit without waiting; queue-full is an immediate failed completion."""

        validate_message(query_message)
        if query_message["message_type"] != "query":
            raise ProtocolStateError("worker submit requires message_type=query")
        if not self.identity.matches(query_message):
            raise ProtocolStateError("worker query identity mismatch")
        if query_message["sender_clock_domain"] != self.clock_domain:
            raise ProtocolStateError("worker query clock domain mismatch")
        if self._thread is None:
            raise AsyncServiceError("worker must be started before submit")
        query_id = str(query_message["query_id"])
        now = (
            self._clock_ns()
            if received_monotonic_ns is None
            else int(received_monotonic_ns)
        )
        raw_hash = raw_query_sha256 or message_sha256(query_message)
        with self._state_lock:
            if query_id in self._seen_query_ids:
                return SubmitDecision(False, "duplicate_query_id", query_id)
            if len(self._seen_query_ids) >= self.limits.max_query_history:
                self._set_fatal_locked("query_history_overflow")
                return SubmitDecision(False, "query_history_overflow", query_id)
            self._seen_query_ids.add(query_id)
        item = _WorkItem(
            connection_id=connection_id,
            query=copy.deepcopy(query_message),
            raw_query_sha256=raw_hash,
            received_monotonic_ns=now,
        )
        immediate = self._preflight_failure(item)
        if immediate is not None:
            self._publish(immediate)
            return SubmitDecision(False, immediate.status, query_id)
        if not self._accepting.is_set() or self._stop.is_set():
            self._publish(
                self._failure(item, "provider_error", "worker is shutting down", True)
            )
            return SubmitDecision(False, "worker_shutting_down", query_id)
        try:
            self._requests.put_nowait(item)
        except queue.Full:
            self._publish(
                self._failure(
                    item,
                    "provider_error",
                    "bounded request queue overflow",
                    True,
                )
            )
            return SubmitDecision(False, "request_queue_overflow", query_id)
        return SubmitDecision(True, "queued", query_id)

    def poll_completed(self, max_items: Optional[int] = None) -> Tuple[PollItem, ...]:
        """Return immediately; this method never invokes a blocking queue operation."""

        limit = self.limits.max_poll_batch if max_items is None else int(max_items)
        if limit < 1:
            raise ValueError("max_items must be >= 1")
        output: List[PollItem] = []
        while len(output) < limit:
            try:
                output.append(self._completions.get_nowait())
            except queue.Empty:
                break
        with self._state_lock:
            if (
                len(output) < limit
                and self._fatal_reason is not None
                and not self._fatal_reported
            ):
                output.append(WorkerFault(self._fatal_reason, self._clock_ns()))
                self._fatal_reported = True
        return tuple(output)

    def shutdown(self, *, wait: bool = False, timeout_s: float = 2.0) -> None:
        self._accepting.clear()
        self._stop.set()
        while True:
            try:
                item = self._requests.get_nowait()
            except queue.Empty:
                break
            self._publish(
                self._failure(
                    item, "provider_error", "worker shut down before compute", True
                )
            )
        thread = self._thread
        if wait and thread is not None:
            thread.join(max(0.0, timeout_s))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._requests.get(timeout=0.05)
            except queue.Empty:
                continue
            completion = self._compute_one(item)
            self._publish(completion)

    def _compute_one(self, item: _WorkItem) -> ComputeCompletion:
        started = self._clock_ns()
        if started >= int(item.query["deadline_monotonic_ns"]):
            return self._failure(
                item,
                "deadline_missed",
                "deadline elapsed before provider compute started",
                False,
                started=started,
            )
        if self._has_stale_pose_at(item.query, started):
            return self._failure(
                item,
                "stale_pose",
                "pose state expired while waiting for provider compute",
                False,
                started=started,
            )
        try:
            physical = self.backend.compute(item.query)
            completed = self._clock_ns()
            if self._stop.is_set():
                return self._failure(
                    item,
                    "provider_error",
                    "worker shut down during compute",
                    True,
                    started=started,
                    completed=completed,
                )
            if completed >= int(item.query["deadline_monotonic_ns"]):
                return self._failure(
                    item,
                    "deadline_missed",
                    "provider compute completed after deadline",
                    False,
                    started=started,
                    completed=completed,
                )
            return ComputeCompletion(
                connection_id=item.connection_id,
                query=item.query,
                raw_query_sha256=item.raw_query_sha256,
                status="ok",
                provider_received_monotonic_ns=item.received_monotonic_ns,
                provider_started_monotonic_ns=started,
                provider_completed_monotonic_ns=completed,
                physical=copy.deepcopy(physical),
            )
        except StalePoseError as exc:
            return self._failure(item, "stale_pose", str(exc), False, started=started)
        except SceneMismatchError as exc:
            return self._failure(
                item, "scene_mismatch", str(exc), False, started=started
            )
        except DeadlineMissedError as exc:
            return self._failure(
                item, "deadline_missed", str(exc), False, started=started
            )
        except Exception as exc:  # provider failure must become a bounded wire error
            return self._failure(
                item,
                "provider_error",
                f"provider compute failed: {exc}",
                True,
                started=started,
            )

    def _preflight_failure(self, item: _WorkItem) -> Optional[ComputeCompletion]:
        query = item.query
        if item.received_monotonic_ns >= int(query["deadline_monotonic_ns"]):
            return self._failure(
                item, "deadline_missed", "query arrived after deadline", False
            )
        poses = list(query["nodes"]) + list(query["jammers"])
        if any(
            bool(pose["stale"])
            or int(pose["freshness_age_ns"]) > self.limits.max_pose_age_ns
            for pose in poses
        ) or self._has_stale_pose_at(query, item.received_monotonic_ns):
            return self._failure(
                item,
                "stale_pose",
                "pose state is stale at provider receive time",
                False,
            )
        actual_scene = query["material_assumptions"]["scene_material_manifest_sha256"]
        if actual_scene != self.scene_material_manifest_sha256:
            return self._failure(
                item,
                "scene_mismatch",
                "query scene material manifest does not match loaded scene",
                False,
            )
        return None

    def _has_stale_pose_at(self, query: Mapping[str, Any], now_ns: int) -> bool:
        poses = list(query["nodes"]) + list(query["jammers"])
        return any(
            now_ns < int(pose["pose_monotonic_ns"])
            or now_ns - int(pose["pose_monotonic_ns"])
            > self.limits.max_pose_age_ns
            for pose in poses
        )

    def _failure(
        self,
        item: _WorkItem,
        status: str,
        detail: str,
        retryable: bool,
        *,
        started: Optional[int] = None,
        completed: Optional[int] = None,
    ) -> ComputeCompletion:
        start = self._clock_ns() if started is None else started
        finish = max(start, self._clock_ns() if completed is None else completed)
        return ComputeCompletion(
            connection_id=item.connection_id,
            query=item.query,
            raw_query_sha256=item.raw_query_sha256,
            status=status,
            provider_received_monotonic_ns=item.received_monotonic_ns,
            provider_started_monotonic_ns=max(item.received_monotonic_ns, start),
            provider_completed_monotonic_ns=max(item.received_monotonic_ns, finish),
            detail=detail[:1024] or status,
            retryable=retryable,
        )

    def _publish(self, completion: ComputeCompletion) -> None:
        try:
            self._completions.put_nowait(completion)
        except queue.Full:
            with self._state_lock:
                self._set_fatal_locked("completion_queue_overflow")

    def _set_fatal_locked(self, reason: str) -> None:
        if self._fatal_reason is None:
            self._fatal_reason = reason
            self._accepting.clear()
            self._stop.set()


@dataclass(frozen=True)
class ProviderServiceConfig:
    identity: ProtocolIdentity
    phase_id: str
    sender_id: str
    clock_domain: str
    executable_path: str
    executable_sha256: str
    scene_path: str
    scene_manifest_sha256: str
    scene_material_manifest_sha256: str
    provider_id: str
    sionna_rt_version: str
    mitsuba_version: str
    provider_mode: str = "real_sionna"
    acceptance_eligible: bool = True


class ExactWireLog:
    """Append exact frame bytes to a binary stream plus a hash/offset index."""

    def __init__(self, directory: Path, *, fsync: bool = False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.data_path = self.directory / "sionna_async_wire.bin"
        self.index_path = self.directory / "sionna_async_wire_index.jsonl"
        self._lock = threading.Lock()
        self._fsync = fsync

    def record(
        self,
        direction: str,
        connection_id: str,
        raw_bytes: bytes,
        monotonic_ns: int,
    ) -> Mapping[str, Any]:
        raw = bytes(raw_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            with self.data_path.open("ab") as data_stream:
                offset = data_stream.tell()
                data_stream.write(raw)
                data_stream.flush()
                if self._fsync:
                    os.fsync(data_stream.fileno())
            record = {
                "connection_id": connection_id,
                "direction": direction,
                "length": len(raw),
                "monotonic_ns": int(monotonic_ns),
                "offset": offset,
                "sha256": digest,
            }
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            with self.index_path.open("a", encoding="utf-8") as index_stream:
                index_stream.write(encoded)
                index_stream.flush()
                if self._fsync:
                    os.fsync(index_stream.fileno())
        return record

    def read_records(self) -> Tuple[Mapping[str, Any], ...]:
        if not self.index_path.exists():
            return ()
        return tuple(
            json.loads(line)
            for line in self.index_path.read_text(encoding="utf-8").splitlines()
            if line
        )

    def read_exact(self, record: Mapping[str, Any]) -> bytes:
        with self.data_path.open("rb") as stream:
            stream.seek(int(record["offset"]))
            return stream.read(int(record["length"]))


_UNIT_TEST_SERVICE_TOKEN = object()


class SionnaAsyncTCPService:
    """Single-active-connection TCP JSONL service with async computation."""

    def __init__(
        self,
        config: ProviderServiceConfig,
        worker: AsyncSionnaWorker,
        wire_log: ExactWireLog,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        limits: Optional[ProtocolLimits] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        _unit_test_token: object = None,
    ) -> None:
        self.config = config
        self.worker = worker
        self.wire_log = wire_log
        self.host = host
        self._requested_port = int(port)
        self.limits = limits or worker.limits
        self._clock_ns = clock_ns
        unit_test = _unit_test_token is _UNIT_TEST_SERVICE_TOKEN
        if unit_test != worker.test_only:
            raise AsyncServiceError("test-only service/worker construction mismatch")
        if not unit_test and (
            config.provider_mode != "real_sionna"
            or not config.acceptance_eligible
            or worker.backend.provider_mode != "real_sionna"
            or not worker.backend.acceptance_eligible
        ):
            raise AsyncServiceError("production TCP service requires real_sionna only")
        if unit_test and config.acceptance_eligible:
            raise AsyncServiceError("unit-test service cannot be acceptance eligible")
        if config.identity != worker.identity:
            raise AsyncServiceError("service and worker protocol identities differ")
        if (
            config.scene_material_manifest_sha256
            != worker.scene_material_manifest_sha256
        ):
            raise AsyncServiceError(
                "service and worker scene material identities differ"
            )
        if config.clock_domain != worker.clock_domain:
            raise AsyncServiceError("service and worker monotonic clock domains differ")
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._wire_sequence = 0
        self._reconnect_generation = -1
        self._adapter_tracker = WireSequenceTracker(config.identity)
        self._active_socket: Optional[socket.socket] = None
        self._fatal_exception: Optional[BaseException] = None

    @classmethod
    def for_unit_tests(
        cls,
        config: ProviderServiceConfig,
        worker: AsyncSionnaWorker,
        wire_log: ExactWireLog,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        limits: Optional[ProtocolLimits] = None,
    ) -> "SionnaAsyncTCPService":
        return cls(
            config,
            worker,
            wire_log,
            host=host,
            port=port,
            limits=limits,
            _unit_test_token=_UNIT_TEST_SERVICE_TOKEN,
        )

    @property
    def port(self) -> int:
        if self._listener is None:
            raise AsyncServiceError("service is not started")
        return int(self._listener.getsockname()[1])

    @property
    def fatal_exception(self) -> Optional[BaseException]:
        return self._fatal_exception

    def start(self) -> None:
        if self._thread is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self._requested_port))
        listener.listen(1)
        listener.settimeout(0.05)
        self._listener = listener
        self.worker.start()
        self._thread = threading.Thread(
            target=self._serve,
            name="sionna-async-tcp-service",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(1.0)

    def stop(self, *, timeout_s: float = 2.0) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, timeout_s))
        self.worker.shutdown(wait=True, timeout_s=timeout_s)

    def _serve(self) -> None:
        self._started.set()
        assert self._listener is not None
        try:
            while not self._stop.is_set():
                try:
                    connection, _address = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                self._reconnect_generation += 1
                connection_id = (
                    f"conn-{self._reconnect_generation}-{uuid.uuid4().hex[:12]}"
                )
                self._active_socket = connection
                try:
                    self._handle_connection(
                        connection, connection_id, self._reconnect_generation
                    )
                finally:
                    self._active_socket = None
                    try:
                        connection.close()
                    except OSError:
                        pass
        except BaseException as exc:  # observable by owner/tests; never silently die
            self._fatal_exception = exc

    def _handle_connection(
        self, connection: socket.socket, connection_id: str, generation: int
    ) -> None:
        connection.settimeout(0.01)
        self._send(connection, connection_id, self._hello(generation))
        self._send(connection, connection_id, self._ready(generation))
        buffer = b""
        close_reason: Optional[str] = None
        while not self._stop.is_set() and close_reason is None:
            try:
                chunk = connection.recv(65_536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > self.limits.max_message_bytes and b"\n" not in buffer:
                    self.wire_log.record(
                        "inbound_partial", connection_id, buffer, self._clock_ns()
                    )
                    close_reason = "oversized_unframed_input"
                    break
            except socket.timeout:
                pass
            except OSError:
                break

            while b"\n" in buffer and close_reason is None:
                frame, buffer = buffer.split(b"\n", 1)
                raw_frame = frame + b"\n"
                self.wire_log.record(
                    "inbound", connection_id, raw_frame, self._clock_ns()
                )
                close_reason = self._handle_inbound(
                    connection, connection_id, generation, raw_frame
                )

            for item in self.worker.poll_completed():
                if isinstance(item, WorkerFault):
                    close_reason = item.reason
                    break
                if item.connection_id != connection_id:
                    continue
                self._send_completion(connection, connection_id, generation, item)
        if buffer:
            self.wire_log.record(
                "inbound_partial", connection_id, buffer, self._clock_ns()
            )
        if close_reason is not None or self._stop.is_set():
            reason = close_reason or "service_shutdown"
            try:
                self._send(
                    connection, connection_id, self._disconnect(generation, reason)
                )
            except OSError:
                pass

    def _handle_inbound(
        self,
        connection: socket.socket,
        connection_id: str,
        generation: int,
        raw_frame: bytes,
    ) -> Optional[str]:
        try:
            message = decode_message(raw_frame, max_bytes=self.limits.max_message_bytes)
            self._adapter_tracker.observe(message)
        except (ProtocolValidationError, ProtocolStateError) as exc:
            self._send(
                connection,
                connection_id,
                self._error(generation, str(exc), raw_frame),
            )
            return "invalid_adapter_message"
        message_type = message["message_type"]
        if message_type in {"hello", "ready"}:
            if message["sender_role"] != "adapter":
                self._send(
                    connection,
                    connection_id,
                    self._error(
                        generation, "peer handshake role must be adapter", raw_frame
                    ),
                )
                return "invalid_adapter_role"
            if message_type == "ready":
                scene = message["scene_identity"]
                if (
                    scene["bundle_id"] != self.config.identity.bundle_id
                    or scene["scene_manifest_sha256"]
                    != self.config.scene_manifest_sha256
                ):
                    self._send(
                        connection,
                        connection_id,
                        self._error(
                            generation, "adapter scene identity mismatch", raw_frame
                        ),
                    )
                    return "scene_mismatch"
            return None
        if message_type == "query":
            try:
                decision = self.worker.submit(
                    message,
                    connection_id=connection_id,
                    received_monotonic_ns=self._clock_ns(),
                    raw_query_sha256=hashlib.sha256(raw_frame).hexdigest(),
                )
            except (ProtocolStateError, ProtocolValidationError) as exc:
                self._send(
                    connection,
                    connection_id,
                    self._error(generation, str(exc), raw_frame),
                )
                return "invalid_query_identity"
            if decision.reason in {"duplicate_query_id", "query_history_overflow"}:
                self._send(
                    connection,
                    connection_id,
                    self._error(generation, decision.reason, raw_frame),
                )
                return decision.reason
            return None
        if message_type == "disconnect":
            return "adapter_disconnected"
        self._send(
            connection,
            connection_id,
            self._error(generation, f"adapter cannot send {message_type}", raw_frame),
        )
        return "unexpected_adapter_message"

    def _send_completion(
        self,
        connection: socket.socket,
        connection_id: str,
        generation: int,
        completion: ComputeCompletion,
    ) -> None:
        try:
            message = self._completion_message(generation, completion)
            encode_message(message, max_bytes=self.limits.max_message_bytes)
        except (ProtocolValidationError, KeyError, TypeError, ValueError) as exc:
            failed = ComputeCompletion(
                connection_id=completion.connection_id,
                query=completion.query,
                raw_query_sha256=completion.raw_query_sha256,
                status="provider_error",
                provider_received_monotonic_ns=completion.provider_received_monotonic_ns,
                provider_started_monotonic_ns=completion.provider_started_monotonic_ns,
                provider_completed_monotonic_ns=max(
                    completion.provider_completed_monotonic_ns, self._clock_ns()
                ),
                detail=f"invalid compute output: {exc}"[:1024],
                retryable=True,
            )
            message = self._completion_message(generation, failed)
        self._send(connection, connection_id, message)

    def _completion_message(
        self, generation: int, completion: ComputeCompletion
    ) -> Mapping[str, Any]:
        query = completion.query
        sent = max(completion.provider_completed_monotonic_ns, self._clock_ns())
        message = self._common(
            "result", generation, phase_id=str(query["phase_id"]), emitted=sent
        )
        message.update(
            {
                "query_id": query["query_id"],
                "node_state_seq": query["node_state_seq"],
                "directed_link_id": query["directed_link_id"],
                "traffic_class": query["traffic_class"],
                "tx_node_id": query["tx_node_id"],
                "rx_node_id": query["rx_node_id"],
                "provider_clock_domain": self.config.clock_domain,
                "provider_received_monotonic_ns": completion.provider_received_monotonic_ns,
                "provider_started_monotonic_ns": completion.provider_started_monotonic_ns,
                "provider_completed_monotonic_ns": completion.provider_completed_monotonic_ns,
                "provider_sent_monotonic_ns": sent,
                "status": completion.status,
            }
        )
        if completion.status == "ok":
            message.update(
                {
                    "validity_clock_domain": self.config.clock_domain,
                    "validity_start_monotonic_ns": completion.provider_completed_monotonic_ns,
                    "expires_monotonic_ns": completion.provider_completed_monotonic_ns
                    + self.limits.validity_ttl_ns,
                    "physical": copy.deepcopy(completion.physical),
                }
            )
        else:
            message["error_body"] = {
                "code": completion.status,
                "detail": completion.detail[:1024] or completion.status,
                "retryable": completion.retryable,
            }
        return message

    def _hello(self, generation: int) -> Mapping[str, Any]:
        message = self._common("hello", generation)
        message.update(self._handshake_fields("initializing"))
        return message

    def _ready(self, generation: int) -> Mapping[str, Any]:
        message = self._common("ready", generation)
        message.update(self._handshake_fields("ready"))
        message["scene_identity"] = {
            "bundle_id": self.config.identity.bundle_id,
            "scene_manifest_sha256": self.config.scene_manifest_sha256,
            "scene_path": self.config.scene_path,
        }
        return message

    def _handshake_fields(self, readiness: str) -> Dict[str, Any]:
        return {
            "protocol_name": "sionna_async",
            "protocol_version": 1,
            "sender_role": "provider",
            "executable_identity": {
                "path": self.config.executable_path,
                "sha256": self.config.executable_sha256,
            },
            "capabilities": {
                "supported_message_types": sorted(MESSAGE_TYPES),
                "max_message_bytes": self.limits.max_message_bytes,
            },
            "accepted_run_id": self.config.identity.run_id,
            "accepted_config_hash": self.config.identity.config_hash,
            "accepted_bundle_id": self.config.identity.bundle_id,
            "readiness_state": readiness,
            "provider_identity": {
                "provider_id": self.config.provider_id,
                "provider_mode": self.config.provider_mode,
                "acceptance_eligible": self.config.acceptance_eligible,
                "sionna_rt_version": self.config.sionna_rt_version,
                "mitsuba_version": self.config.mitsuba_version,
            },
        }

    def _error(
        self, generation: int, reason: str, rejected_raw: bytes
    ) -> Mapping[str, Any]:
        now = self._clock_ns()
        message = self._common("error", generation, emitted=now)
        message.update(
            {
                "error_kind": "invalid_request",
                "reason": (reason or "invalid request")[:1024],
                "lifecycle_monotonic_ns": now,
                "rejected_request_sha256": hashlib.sha256(rejected_raw).hexdigest(),
            }
        )
        try:
            decoded = decode_message(rejected_raw)
            message["rejected_wire_sequence"] = decoded["wire_sequence"]
        except ProtocolValidationError:
            pass
        return message

    def _disconnect(self, generation: int, reason: str) -> Mapping[str, Any]:
        now = self._clock_ns()
        message = self._common("disconnect", generation, emitted=now)
        message.update(
            {
                "disconnect_kind": "disconnected",
                "reason": (reason or "disconnected")[:1024],
                "lifecycle_monotonic_ns": now,
            }
        )
        return message

    def _common(
        self,
        message_type: str,
        generation: int,
        *,
        phase_id: Optional[str] = None,
        emitted: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._wire_sequence += 1
        identity = self.config.identity
        return {
            "schema_version": 1,
            "message_type": message_type,
            "wire_sequence": self._wire_sequence,
            "sender_id": self.config.sender_id,
            "run_id": identity.run_id,
            "profile": identity.profile,
            "phase_id": phase_id or self.config.phase_id,
            "contract_hash": identity.contract_hash,
            "config_hash": identity.config_hash,
            "bundle_id": identity.bundle_id,
            "reconnect_generation": generation,
            "sender_clock_domain": self.config.clock_domain,
            "emitted_monotonic_ns": self._clock_ns() if emitted is None else emitted,
        }

    def _send(
        self,
        connection: socket.socket,
        connection_id: str,
        message: Mapping[str, Any],
    ) -> None:
        raw = encode_message(message, max_bytes=self.limits.max_message_bytes)
        self.wire_log.record("outbound", connection_id, raw, self._clock_ns())
        connection.sendall(raw)


def create_production_worker(
    *,
    runtime_files: Any,
    identity: ProtocolIdentity,
    scene_material_manifest_sha256: str,
    limits: Optional[ProtocolLimits] = None,
    clock_domain: str = "host-monotonic",
) -> AsyncSionnaWorker:
    """Production factory with no analytic/fake fallback path."""

    from network.radio_provider import provider as provider_module

    settings = provider_module.load_settings(runtime_files, "real_sionna")
    if settings.mode != "real_sionna":
        raise AsyncServiceError("production factory refused non-real provider settings")
    instance = provider_module.SionnaRadioProvider(settings)
    backend = RealSionnaBackend(instance)
    return AsyncSionnaWorker(
        backend,
        identity,
        scene_material_manifest_sha256,
        limits=limits,
        clock_domain=clock_domain,
    )


def create_production_service(
    *,
    runtime_files: Any,
    config: ProviderServiceConfig,
    wire_log: ExactWireLog,
    host: str = "127.0.0.1",
    port: int = 0,
    limits: Optional[ProtocolLimits] = None,
) -> SionnaAsyncTCPService:
    """Build the TCP service through the strict real-Sionna production path."""

    if config.provider_mode != "real_sionna" or not config.acceptance_eligible:
        raise AsyncServiceError(
            "production service config must claim eligible real_sionna"
        )
    worker = create_production_worker(
        runtime_files=runtime_files,
        identity=config.identity,
        scene_material_manifest_sha256=config.scene_material_manifest_sha256,
        limits=limits,
        clock_domain=config.clock_domain,
    )
    return SionnaAsyncTCPService(
        config,
        worker,
        wire_log,
        host=host,
        port=port,
        limits=limits,
    )


__all__ = [
    "AsyncServiceError",
    "AsyncSionnaWorker",
    "ComputeCompletion",
    "DeadlineMissedError",
    "ExactWireLog",
    "ProviderServiceConfig",
    "RealSionnaBackend",
    "SceneMismatchError",
    "SionnaAsyncTCPService",
    "StalePoseError",
    "SubmitDecision",
    "WorkerFault",
    "create_production_service",
    "create_production_worker",
]
