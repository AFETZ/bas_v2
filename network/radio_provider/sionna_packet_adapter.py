#!/usr/bin/env python3
"""Non-blocking packet-event adapter between ns-3 and Sionna async v1.

The adapter tails factual ns-3 packet events, coalesces at most one outstanding
query per directed-link/traffic-class cell, and never waits for Sionna on the
packet-event path.  Fresh results are selected by ``DirectedLinkStateManager``
and published as append-only IPC state updates consumed by the packet engine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import queue
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableSet,
    Optional,
    Protocol,
    Tuple,
    Union,
)

from network.radio_provider.sionna_async import (
    DirectedLinkStateManager,
    ProtocolIdentity,
    ProtocolLimits,
    ProtocolStateError,
    ProtocolValidationError,
    WireSequenceTracker,
    decode_message,
    encode_message,
    node_state_sha256,
    validate_message,
)
from network.radio_provider.sionna_async_service import ExactWireLog


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EFFECTS_PATH = ROOT / "network/config/sionna_packet_effects_v1.json"
PACKET_EVENT_SCHEMA = "ams.ns3.packet_event/v1"
STATE_IPC_SCHEMA = "ams.sionna.packet_state/v1"
ADAPTER_AUDIT_SCHEMA = "ams.sionna.packet_adapter_event/v1"
CELL_RE = re.compile(r"^(cp>uav[1-5]|uav[1-5]>cp)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]*$")
TRAFFIC_CLASSES = ("control", "payload", "additional_data")


class PacketAdapterError(RuntimeError):
    """Adapter input or state is unsafe and cannot be silently accepted."""


@dataclass(frozen=True)
class PacketEffectsPolicy:
    mapping_version: str
    max_effect_delay_ns: int
    curves: Mapping[str, Tuple[Tuple[float, float], ...]]
    service_rate_tiers: Tuple[Tuple[float, int], ...]

    @classmethod
    def load(cls, path: Path = DEFAULT_EFFECTS_PATH) -> "PacketEffectsPolicy":
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PacketAdapterError(
                f"cannot load effects policy {path}: {exc}"
            ) from exc
        _exact_keys(
            raw,
            "effects",
            {
                "contract",
                "deterministic_loss",
                "engineering_basis",
                "max_effect_delay_ns",
                "mapping_version",
                "service_rate_mapping",
                "traffic_classes",
            },
        )
        if raw["contract"] != "ams.sionna_packet_effects/v2":
            raise PacketAdapterError("effects contract mismatch")
        deterministic = raw["deterministic_loss"]
        _exact_keys(
            deterministic,
            "effects.deterministic_loss",
            {"hash_algorithm", "sample_domain", "sample_precision_bits"},
        )
        if deterministic != {
            "hash_algorithm": "sha256",
            "sample_domain": "packet_transport_payload_sha256+applied_state_id+mapping_seed",
            "sample_precision_bits": 53,
        }:
            raise PacketAdapterError("deterministic loss contract mismatch")
        engineering_basis = _exact_keys(
            raw["engineering_basis"],
            "effects.engineering_basis",
            {
                "calibration_note",
                "channel_capacity_bps",
                "limitations",
                "loss_probability_units",
                "propagation_delay_units",
                "service_rate_source",
                "service_rate_units",
                "sinr_units",
            },
        )
        expected_engineering_basis = {
            "calibration_note": (
                "Deterministic integration profile only; thresholds and PER proxies "
                "are not calibrated modem or PHY performance claims."
            ),
            "channel_capacity_bps": 20_000_000,
            "limitations": [
                "No coding, MCS, HARQ, fading, Doppler, antenna-pattern gain, or "
                "receiver implementation is inferred.",
                "Selected service rate never exceeds the configured shared-medium "
                "channel capacity.",
                "PER curves are traffic-class engineering proxies applied to "
                "independent deterministic packet samples.",
            ],
            "loss_probability_units": "1",
            "propagation_delay_units": "ns",
            "service_rate_source": (
                "network/config/service_tiers.yaml frozen M4 engineering thresholds"
            ),
            "service_rate_units": "bit/s",
            "sinr_units": "dB",
        }
        if dict(engineering_basis) != expected_engineering_basis:
            raise PacketAdapterError("effects engineering basis differs from frozen M4 basis")
        mapping_version = _safe_id(raw["mapping_version"], "effects.mapping_version")
        if mapping_version != "sinr-rate-per-v2":
            raise PacketAdapterError("effects mapping version mismatch")
        maximum = _integer(raw["max_effect_delay_ns"], "effects.max_effect_delay_ns", 1)
        rate_mapping = _exact_keys(
            raw["service_rate_mapping"],
            "effects.service_rate_mapping",
            {"basis", "boundary_policy", "tiers_descending", "units"},
        )
        frozen_tiers = (
            (20.0, 20_000_000),
            (11.0, 2_000_000),
            (6.0, 500_000),
            (0.0, 100_000),
            (-4.0, 10_000),
            (-8.0, 1_000),
            (-999.0, 0),
        )
        raw_tiers = rate_mapping.get("tiers_descending")
        if (
            rate_mapping.get("basis") != "sinr_db"
            or rate_mapping.get("boundary_policy")
            != "descending_first_match_inclusive_lower_bound"
            or rate_mapping.get("units") != {"rate": "bit/s", "sinr": "dB"}
            or not isinstance(raw_tiers, list)
        ):
            raise PacketAdapterError("service-rate mapping envelope differs")
        parsed_tiers: List[Tuple[float, int]] = []
        for index, point in enumerate(raw_tiers):
            if not isinstance(point, list) or len(point) != 2:
                raise PacketAdapterError(f"service tier {index} must be [sinr, rate]")
            threshold = _finite(point[0], f"service tier {index}.sinr")
            rate = _integer(point[1], f"service tier {index}.rate", 0)
            parsed_tiers.append((threshold, rate))
        if tuple(parsed_tiers) != frozen_tiers:
            raise PacketAdapterError("service-rate tiers differ from frozen 7-tier mapping")
        traffic = raw["traffic_classes"]
        _exact_keys(traffic, "effects.traffic_classes", set(TRAFFIC_CLASSES))
        curves: Dict[str, Tuple[Tuple[float, float], ...]] = {}
        for traffic_class in TRAFFIC_CLASSES:
            points = traffic[traffic_class]
            if not isinstance(points, list) or len(points) < 2:
                raise PacketAdapterError(
                    f"{traffic_class} loss curve needs at least two points"
                )
            parsed: List[Tuple[float, float]] = []
            for index, point in enumerate(points):
                if not isinstance(point, list) or len(point) != 2:
                    raise PacketAdapterError(
                        f"{traffic_class}[{index}] must be [sinr_db, loss]"
                    )
                sinr = _finite(point[0], f"{traffic_class}[{index}].sinr")
                loss = _finite(point[1], f"{traffic_class}[{index}].loss")
                if not 0.0 <= loss <= 1.0:
                    raise PacketAdapterError("loss probability must be in [0,1]")
                parsed.append((sinr, loss))
            if any(
                parsed[index][0] >= parsed[index + 1][0]
                for index in range(len(parsed) - 1)
            ):
                raise PacketAdapterError(
                    f"{traffic_class} SINR points must increase strictly"
                )
            if any(
                parsed[index][1] < parsed[index + 1][1]
                for index in range(len(parsed) - 1)
            ):
                raise PacketAdapterError(
                    f"{traffic_class} loss must not rise with SINR"
                )
            curves[traffic_class] = tuple(parsed)
        return cls(mapping_version, maximum, curves, frozen_tiers)

    def map_physical(
        self, physical: Mapping[str, Any], traffic_class: str
    ) -> "MappedEffects":
        if traffic_class not in self.curves:
            raise PacketAdapterError(f"unmapped traffic class: {traffic_class}")
        sinr = _finite(physical.get("sinr_db"), "physical.sinr_db")
        propagation = _finite(
            physical.get("propagation_delay_ns"), "physical.propagation_delay_ns"
        )
        if propagation < 0 or propagation > self.max_effect_delay_ns:
            raise PacketAdapterError(
                "physical propagation delay outside configured bound"
            )
        curve = self.curves[traffic_class]
        if sinr <= curve[0][0]:
            probability = curve[0][1]
        elif sinr >= curve[-1][0]:
            probability = curve[-1][1]
        else:
            probability = 1.0
            for (left_sinr, left_loss), (right_sinr, right_loss) in zip(
                curve, curve[1:]
            ):
                if left_sinr <= sinr <= right_sinr:
                    fraction = (sinr - left_sinr) / (right_sinr - left_sinr)
                    probability = left_loss + fraction * (right_loss - left_loss)
                    break
        service_rate_bps = 0
        for threshold, rate in self.service_rate_tiers:
            if sinr >= threshold:
                service_rate_bps = rate
                break
        return MappedEffects(
            propagation_delay_ns=int(round(propagation)),
            loss_probability=max(0.0, min(1.0, probability)),
            service_rate_bps=service_rate_bps,
            mapping_version=self.mapping_version,
        )


@dataclass(frozen=True)
class MappedEffects:
    propagation_delay_ns: int
    loss_probability: float
    service_rate_bps: int
    mapping_version: str


def deterministic_loss_sample(
    packet_causal_sha256: str, applied_state_id: str, mapping_seed: int
) -> float:
    _sha256(packet_causal_sha256, "packet_causal_sha256")
    _safe_id(applied_state_id, "applied_state_id")
    seed = _integer(mapping_seed, "mapping_seed", 0)
    material = f"{packet_causal_sha256}\0{applied_state_id}\0{seed}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    numerator = int.from_bytes(digest[:8], "big") >> 11
    return numerator / float(1 << 53)


def packet_delivery_decision(
    *,
    packet_causal_sha256: str,
    applied_state_id: str,
    mapping_seed: int,
    loss_probability: float,
    intervention: str = "natural",
) -> Tuple[float, bool]:
    probability = _finite(loss_probability, "loss_probability")
    if not 0.0 <= probability <= 1.0:
        raise PacketAdapterError("loss_probability must be in [0,1]")
    if intervention not in {"natural", "force_drop", "force_deliver"}:
        raise PacketAdapterError("unsupported causal intervention")
    sample = deterministic_loss_sample(
        packet_causal_sha256, applied_state_id, mapping_seed
    )
    if intervention == "force_drop":
        return sample, False
    if intervention == "force_deliver":
        return sample, True
    return sample, sample >= probability


class AppendOnlyJsonl:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> bytes:
        raw = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        with self._lock:
            with self.path.open("ab") as stream:
                stream.write(raw)
                stream.flush()
        return raw


class AppliedStateIPCWriter:
    """Emit bounded-size, self-hashed state updates for the C++ engine."""

    def __init__(self, path: Path, max_line_bytes: int = 65_536):
        self._output = AppendOnlyJsonl(path)
        self.max_line_bytes = max_line_bytes
        self._sequence = 0

    @property
    def path(self) -> Path:
        return self._output.path

    def write(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._sequence += 1
        record = {
            "schema": STATE_IPC_SCHEMA,
            "state_sequence": self._sequence,
            **copy.deepcopy(payload),
        }
        if "state_sha256" in record:
            raise PacketAdapterError("caller must not supply state_sha256")
        canonical = json.dumps(
            record, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        record["state_sha256"] = hashlib.sha256(canonical).hexdigest()
        encoded = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        if len(encoded) > self.max_line_bytes:
            raise PacketAdapterError("state IPC record exceeds bounded line size")
        self._output.append(record)
        return record


class PacketEventTailer:
    """Append-only file tailer; ``poll`` performs no waiting and has hard bounds."""

    def __init__(
        self,
        path: Path,
        *,
        max_line_bytes: int = 65_536,
        max_buffer_bytes: int = 1_048_576,
    ):
        self.path = Path(path)
        self.max_line_bytes = max_line_bytes
        self.max_buffer_bytes = max_buffer_bytes
        self._offset = 0
        self._partial = b""

    def poll(self, max_records: int = 64) -> Tuple[Mapping[str, Any], ...]:
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        if not self.path.exists():
            return ()
        size = self.path.stat().st_size
        if size < self._offset:
            raise PacketAdapterError("packet event file truncated or replaced")
        with self.path.open("rb") as stream:
            stream.seek(self._offset)
            chunk = stream.read(
                min(self.max_buffer_bytes - len(self._partial), size - self._offset)
            )
        self._offset += len(chunk)
        self._partial += chunk
        if len(self._partial) >= self.max_buffer_bytes and b"\n" not in self._partial:
            raise PacketAdapterError("packet event tail buffer overflow")
        records: List[Mapping[str, Any]] = []
        while len(records) < max_records and b"\n" in self._partial:
            line, self._partial = self._partial.split(b"\n", 1)
            if len(line) + 1 > self.max_line_bytes:
                raise PacketAdapterError("packet event line exceeds bound")
            try:
                value = json.loads(
                    line.decode("utf-8"), object_pairs_hook=_unique_object
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PacketAdapterError(f"invalid packet event JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise PacketAdapterError("packet event line must be an object")
            records.append(value)
        return tuple(records)


@dataclass(frozen=True)
class ClientFault:
    reason: str


ClientPollItem = Union[bytes, ClientFault]


@dataclass(frozen=True)
class AdapterClientConfig:
    identity: ProtocolIdentity
    sender_id: str
    phase_id: str
    clock_domain: str
    executable_path: str
    executable_sha256: str
    scene_path: str
    scene_manifest_sha256: str


class QueryTransport(Protocol):
    def reserve_query_envelope(self) -> Optional[Tuple[int, int]]: ...

    def submit_query(self, query_message: Mapping[str, Any]) -> bool: ...

    def poll_results(self, max_items: int = 64) -> Tuple[ClientPollItem, ...]: ...


class SupervisedResultFaultInjector:
    """Delay/release/duplicate captured real results without changing one byte.

    This wrapper is inert until explicitly armed.  It exists solely for the
    predeclared M4 F-expiry exercise and keeps hard bounds on held/released
    frames.  Normal production traffic still uses one outstanding query per
    cell in :class:`PacketSionnaAdapter`.
    """

    def __init__(
        self,
        transport: QueryTransport,
        audit_path: Path,
        *,
        max_held_results: int = 2,
        max_release_queue: int = 8,
        max_captured_results: int = 64,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            not 1 <= max_held_results <= 8
            or not 1 <= max_release_queue <= 64
            or not max_held_results < max_captured_results <= 1024
        ):
            raise PacketAdapterError("fault injector bounds are invalid")
        self.transport = transport
        self._audit = AppendOnlyJsonl(audit_path)
        self._max_held = max_held_results
        self._max_captured = max_captured_results
        self._released: queue.Queue[ClientPollItem] = queue.Queue(max_release_queue)
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._sequence = 0
        self._armed_links: set[str] = set()
        self._held: Dict[str, bytes] = {}
        self._captured: Dict[str, bytes] = {}
        self._captured_high_watermark = 0
        self._captured_evictions = 0
        self._captured_overflows = 0
        self._held_overflows = 0
        self._release_queue_overflows = 0

    def reserve_query_envelope(self) -> Optional[Tuple[int, int]]:
        return self.transport.reserve_query_envelope()

    def submit_query(self, query_message: Mapping[str, Any]) -> bool:
        return self.transport.submit_query(query_message)

    def arm_hold_next(self, directed_link_id: str) -> None:
        _safe_id(directed_link_id, "fault.directed_link_id")
        with self._lock:
            if directed_link_id in self._armed_links:
                raise PacketAdapterError("fault hold is already armed for link")
            self._armed_links.add(directed_link_id)
            self._record("hold_armed", None, None, directed_link_id)

    @property
    def held_query_ids(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._held))

    def held_query_ids_for_link(self, directed_link_id: str) -> Tuple[str, ...]:
        """Return held real-result IDs for one exact link in arrival order."""

        _safe_id(directed_link_id, "fault.directed_link_id")
        with self._lock:
            return tuple(
                query_id
                for query_id, raw in self._held.items()
                if decode_message(raw).get("directed_link_id") == directed_link_id
            )

    @property
    def captured_query_ids(self) -> Tuple[str, ...]:
        """Return the bounded real-result capture index in arrival order."""

        with self._lock:
            return tuple(self._captured)

    def latest_captured_query_id(self, directed_link_id: str) -> Optional[str]:
        """Select the newest captured real result for one exact directed link."""

        _safe_id(directed_link_id, "fault.directed_link_id")
        with self._lock:
            for query_id in reversed(tuple(self._captured)):
                message = decode_message(self._captured[query_id])
                if message.get("directed_link_id") == directed_link_id:
                    return query_id
        return None

    @property
    def statistics(self) -> Mapping[str, int]:
        with self._lock:
            return dict(self._statistics())

    def release_held(self, query_id: str) -> None:
        with self._lock:
            raw = self._held.get(query_id)
            if raw is None:
                raise PacketAdapterError("requested held result does not exist")
            try:
                self._released.put_nowait(raw)
            except queue.Full as exc:
                self._release_queue_overflows += 1
                message = decode_message(raw)
                self._record(
                    "release_queue_overflow",
                    query_id,
                    hashlib.sha256(raw).hexdigest(),
                    str(message["directed_link_id"]),
                )
                raise PacketAdapterError("fault release queue overflow") from exc
            del self._held[query_id]
            message = decode_message(raw)
            self._record(
                "held_result_released",
                query_id,
                hashlib.sha256(raw).hexdigest(),
                str(message["directed_link_id"]),
            )

    def inject_duplicate(self, query_id: str) -> None:
        with self._lock:
            raw = self._captured.get(query_id)
            if raw is None:
                raise PacketAdapterError("captured result is unavailable for duplicate")
            try:
                self._released.put_nowait(raw)
            except queue.Full as exc:
                self._release_queue_overflows += 1
                message = decode_message(raw)
                self._record(
                    "release_queue_overflow",
                    query_id,
                    hashlib.sha256(raw).hexdigest(),
                    str(message["directed_link_id"]),
                )
                raise PacketAdapterError("fault release queue overflow") from exc
            message = decode_message(raw)
            self._record(
                "byte_identical_duplicate_released",
                query_id,
                hashlib.sha256(raw).hexdigest(),
                str(message["directed_link_id"]),
            )

    def poll_results(self, max_items: int = 64) -> Tuple[ClientPollItem, ...]:
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        output: List[ClientPollItem] = []
        while len(output) < max_items:
            try:
                output.append(self._released.get_nowait())
            except queue.Empty:
                break
        remaining = max_items - len(output)
        source_items = self.transport.poll_results(remaining) if remaining else ()
        for item in source_items:
            if isinstance(item, ClientFault):
                output.append(item)
                continue
            message = decode_message(item)
            query_id = str(message["query_id"])
            link_id = str(message["directed_link_id"])
            digest = hashlib.sha256(item).hexdigest()
            with self._lock:
                self._capture(query_id, bytes(item), link_id)
                if link_id in self._armed_links:
                    self._armed_links.remove(link_id)
                    if len(self._held) >= self._max_held:
                        self._held_overflows += 1
                        self._record(
                            "held_result_overflow", query_id, digest, link_id
                        )
                        raise PacketAdapterError("fault held-result bound exceeded")
                    self._held[query_id] = bytes(item)
                    self._record("real_result_held", query_id, digest, link_id)
                    continue
            output.append(item)
        return tuple(output)

    def _capture(self, query_id: str, raw: bytes, directed_link_id: str) -> None:
        existing = self._captured.get(query_id)
        if existing is not None:
            if existing != raw:
                self._captured_overflows += 1
                self._record(
                    "captured_result_conflict",
                    query_id,
                    hashlib.sha256(raw).hexdigest(),
                    directed_link_id,
                )
                raise PacketAdapterError("conflicting captured result bytes")
            return
        if len(self._captured) >= self._max_captured:
            evicted = next(
                (candidate for candidate in self._captured if candidate not in self._held),
                None,
            )
            if evicted is None:
                self._captured_overflows += 1
                self._record(
                    "captured_result_overflow",
                    query_id,
                    hashlib.sha256(raw).hexdigest(),
                    directed_link_id,
                )
                raise PacketAdapterError("fault captured-result bound exceeded")
            evicted_raw = self._captured.pop(evicted)
            evicted_message = decode_message(evicted_raw)
            self._captured_evictions += 1
            self._record(
                "captured_result_evicted",
                evicted,
                hashlib.sha256(evicted_raw).hexdigest(),
                str(evicted_message["directed_link_id"]),
            )
        self._captured[query_id] = raw
        self._captured_high_watermark = max(
            self._captured_high_watermark, len(self._captured)
        )

    def _statistics(self) -> Mapping[str, int]:
        return {
            "captured_count": len(self._captured),
            "captured_evictions": self._captured_evictions,
            "captured_high_watermark": self._captured_high_watermark,
            "captured_overflows": self._captured_overflows,
            "held_count": len(self._held),
            "held_overflows": self._held_overflows,
            "max_captured_results": self._max_captured,
            "max_held_results": self._max_held,
            "max_release_queue": self._released.maxsize,
            "release_queue_overflows": self._release_queue_overflows,
            "release_queue_size": self._released.qsize(),
        }

    def _record(
        self,
        event: str,
        query_id: Optional[str],
        result_wire_sha256: Optional[str],
        directed_link_id: str,
    ) -> None:
        self._sequence += 1
        self._audit.append(
            {
                "schema": "ams.sionna.result_fault_event/v2",
                "fault_sequence": self._sequence,
                "monotonic_ns": self._clock_ns(),
                "event": event,
                "directed_link_id": directed_link_id,
                "query_id": query_id,
                "result_wire_sha256": result_wire_sha256,
                "payload_policy": "byte_identical_real_provider_result",
                "bounded_state": dict(self._statistics()),
            }
        )


class SionnaAsyncTCPClient:
    """Background TCP client; adapter-facing methods are all non-blocking."""

    def __init__(
        self,
        host: str,
        port: int,
        config: AdapterClientConfig,
        wire_log: ExactWireLog,
        *,
        limits: Optional[ProtocolLimits] = None,
        reconnect_backoff_s: float = 0.05,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.config = config
        self.wire_log = wire_log
        self.limits = limits or ProtocolLimits()
        self.reconnect_backoff_s = reconnect_backoff_s
        self._outbound: queue.Queue[bytes] = queue.Queue(
            self.limits.request_queue_capacity
        )
        self._inbound: queue.Queue[ClientPollItem] = queue.Queue(
            self.limits.completion_queue_capacity
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._wire_sequence = 0
        self._generation = -1
        self._fault: Optional[str] = None
        self._fault_reported = False
        self._provider_tracker = WireSequenceTracker(config.identity)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="sionna-async-adapter-client", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        self._ready.clear()
        if self._thread is not None:
            self._thread.join(max(0.0, timeout_s))

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._fault is None

    def reserve_query_envelope(self) -> Optional[Tuple[int, int]]:
        if not self._ready.is_set() or self._fault is not None:
            return None
        with self._lock:
            self._wire_sequence += 1
            return self._wire_sequence, self._generation

    def submit_query(self, query_message: Mapping[str, Any]) -> bool:
        validate_message(query_message)
        if query_message["message_type"] != "query" or not self.config.identity.matches(
            query_message
        ):
            raise ProtocolStateError("client submit requires matching query message")
        if not self._ready.is_set():
            return False
        with self._lock:
            if int(query_message["reconnect_generation"]) != self._generation:
                return False
        try:
            self._outbound.put_nowait(
                encode_message(query_message, self.limits.max_message_bytes)
            )
        except queue.Full:
            return False
        return True

    def poll_results(self, max_items: int = 64) -> Tuple[ClientPollItem, ...]:
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        output: List[ClientPollItem] = []
        while len(output) < max_items:
            try:
                output.append(self._inbound.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            if (
                len(output) < max_items
                and self._fault is not None
                and not self._fault_reported
            ):
                output.append(ClientFault(self._fault))
                self._fault_reported = True
        return tuple(output)

    def _next_lifecycle_envelope(self) -> Tuple[int, int]:
        with self._lock:
            self._wire_sequence += 1
            return self._wire_sequence, self._generation

    def _run(self) -> None:
        while not self._stop.is_set() and self._fault is None:
            try:
                self._connect_once()
            except (
                OSError,
                ProtocolValidationError,
                ProtocolStateError,
                PacketAdapterError,
            ) as exc:
                self._publish_client_fault(f"provider_connection_failed:{exc}")
            finally:
                self._ready.clear()
            if not self._stop.wait(self.reconnect_backoff_s):
                continue
            if self._stop.is_set():
                break

    def _connect_once(self) -> None:
        with self._lock:
            self._generation += 1
            generation = self._generation
        connection_id = f"adapter-{generation}-{uuid.uuid4().hex[:12]}"
        with socket.create_connection((self.host, self.port), timeout=1.0) as sock:
            sock.settimeout(0.02)
            buffer = b""
            provider_messages: List[Mapping[str, Any]] = []
            deadline = time.monotonic() + 2.0
            while len(provider_messages) < 2:
                if time.monotonic() >= deadline:
                    raise PacketAdapterError("provider handshake timeout")
                try:
                    chunk = sock.recv(65_536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise PacketAdapterError("provider closed during handshake")
                buffer += chunk
                while b"\n" in buffer and len(provider_messages) < 2:
                    line, buffer = buffer.split(b"\n", 1)
                    raw = line + b"\n"
                    self.wire_log.record(
                        "inbound", connection_id, raw, time.monotonic_ns()
                    )
                    message = decode_message(raw, self.limits.max_message_bytes)
                    self._provider_tracker.observe(message)
                    provider_messages.append(message)
            provider_hello, provider_ready = provider_messages
            if [item["message_type"] for item in provider_messages] != [
                "hello",
                "ready",
            ]:
                raise PacketAdapterError("provider must send hello then ready")
            provider_identity = provider_hello.get("provider_identity", {})
            if provider_identity.get(
                "provider_mode"
            ) != "real_sionna" or not provider_identity.get(
                "acceptance_eligible", False
            ):
                raise PacketAdapterError(
                    "adapter refused non-real/non-eligible provider"
                )
            scene = provider_ready["scene_identity"]
            if (
                scene["bundle_id"] != self.config.identity.bundle_id
                or scene["scene_manifest_sha256"] != self.config.scene_manifest_sha256
            ):
                raise PacketAdapterError("provider scene identity mismatch")
            for message in (self._hello(generation), self._ready_message(generation)):
                raw = encode_message(message, self.limits.max_message_bytes)
                sock.sendall(raw)
                self.wire_log.record(
                    "outbound", connection_id, raw, time.monotonic_ns()
                )
            self._ready.set()
            while not self._stop.is_set():
                try:
                    while True:
                        raw = self._outbound.get_nowait()
                        decoded = decode_message(raw, self.limits.max_message_bytes)
                        if int(decoded["reconnect_generation"]) != generation:
                            continue
                        sock.sendall(raw)
                        self.wire_log.record(
                            "outbound", connection_id, raw, time.monotonic_ns()
                        )
                except queue.Empty:
                    pass
                try:
                    chunk = sock.recv(65_536)
                    if not chunk:
                        self._publish_client_fault("provider_disconnected")
                        return
                    buffer += chunk
                except socket.timeout:
                    chunk = b""
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    raw = line + b"\n"
                    self.wire_log.record(
                        "inbound", connection_id, raw, time.monotonic_ns()
                    )
                    message = decode_message(raw, self.limits.max_message_bytes)
                    self._provider_tracker.observe(message)
                    if message["message_type"] == "result":
                        try:
                            self._inbound.put_nowait(raw)
                        except queue.Full:
                            self._set_fault("client_completion_queue_overflow")
                            return
                    elif message["message_type"] in {"error", "disconnect"}:
                        self._publish_client_fault(
                            f"provider_{message['message_type']}:{message.get('reason', 'no_reason')}"
                        )
                        return
            self._ready.clear()

    def _common(
        self, message_type: str, sequence: int, generation: int
    ) -> Dict[str, Any]:
        identity = self.config.identity
        return {
            "schema_version": 1,
            "message_type": message_type,
            "wire_sequence": sequence,
            "sender_id": self.config.sender_id,
            "run_id": identity.run_id,
            "profile": identity.profile,
            "phase_id": self.config.phase_id,
            "contract_hash": identity.contract_hash,
            "config_hash": identity.config_hash,
            "bundle_id": identity.bundle_id,
            "reconnect_generation": generation,
            "sender_clock_domain": self.config.clock_domain,
            "emitted_monotonic_ns": time.monotonic_ns(),
        }

    def _hello(self, generation: int) -> Mapping[str, Any]:
        sequence, current = self._next_lifecycle_envelope()
        if current != generation:
            raise PacketAdapterError(
                "adapter reconnect generation changed during hello"
            )
        message = self._common("hello", sequence, generation)
        message.update(self._handshake_fields("initializing"))
        return message

    def _ready_message(self, generation: int) -> Mapping[str, Any]:
        sequence, current = self._next_lifecycle_envelope()
        if current != generation:
            raise PacketAdapterError(
                "adapter reconnect generation changed during ready"
            )
        message = self._common("ready", sequence, generation)
        message.update(self._handshake_fields("ready"))
        message["scene_identity"] = {
            "bundle_id": self.config.identity.bundle_id,
            "scene_manifest_sha256": self.config.scene_manifest_sha256,
            "scene_path": self.config.scene_path,
        }
        return message

    def _handshake_fields(self, readiness: str) -> Mapping[str, Any]:
        identity = self.config.identity
        return {
            "protocol_name": "sionna_async",
            "protocol_version": 1,
            "sender_role": "adapter",
            "executable_identity": {
                "path": self.config.executable_path,
                "sha256": self.config.executable_sha256,
            },
            "capabilities": {
                "supported_message_types": [
                    "hello",
                    "ready",
                    "query",
                    "result",
                    "error",
                    "disconnect",
                ],
                "max_message_bytes": self.limits.max_message_bytes,
            },
            "accepted_run_id": identity.run_id,
            "accepted_config_hash": identity.config_hash,
            "accepted_bundle_id": identity.bundle_id,
            "readiness_state": readiness,
        }

    def _set_fault(self, reason: str) -> None:
        with self._lock:
            if self._fault is None:
                self._fault = reason
                self._ready.clear()

    def _publish_client_fault(self, reason: str) -> None:
        try:
            self._inbound.put_nowait(ClientFault(reason[:1024]))
        except queue.Full:
            self._set_fault("client_completion_queue_overflow")


@dataclass(frozen=True)
class PoseSnapshot:
    snapshot_sequence: int
    snapshot_monotonic_ns: int
    snapshot_sha256: str
    source_frame: str
    transform_version: str
    nodes: Tuple[Mapping[str, Any], ...]
    jammers: Tuple[Mapping[str, Any], ...]

    @classmethod
    def create(
        cls,
        *,
        snapshot_sequence: int,
        snapshot_monotonic_ns: int,
        source_frame: str,
        transform_version: str,
        nodes: Tuple[Mapping[str, Any], ...],
        jammers: Tuple[Mapping[str, Any], ...],
    ) -> "PoseSnapshot":
        digest = node_state_sha256(
            node_state_seq=snapshot_sequence,
            snapshot_monotonic_ns=snapshot_monotonic_ns,
            source_frame=source_frame,
            transform_version=transform_version,
            nodes=nodes,
            jammers=jammers,
        )
        return cls(
            snapshot_sequence,
            snapshot_monotonic_ns,
            digest,
            source_frame,
            transform_version,
            nodes,
            jammers,
        )

    def __post_init__(self) -> None:
        _integer(self.snapshot_sequence, "snapshot_sequence", 1)
        _integer(self.snapshot_monotonic_ns, "snapshot_monotonic_ns", 0)
        _sha256(self.snapshot_sha256, "snapshot_sha256")
        expected = node_state_sha256(
            node_state_seq=self.snapshot_sequence,
            snapshot_monotonic_ns=self.snapshot_monotonic_ns,
            source_frame=self.source_frame,
            transform_version=self.transform_version,
            nodes=self.nodes,
            jammers=self.jammers,
        )
        if self.snapshot_sha256 != expected:
            raise PacketAdapterError("PoseSnapshot hash does not match immutable content")

    def refreshed(
        self, now_ns: int, max_pose_age_ns: int
    ) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
        nodes: List[Mapping[str, Any]] = []
        for source in self.nodes:
            item = copy.deepcopy(source)
            age = now_ns - int(item["pose_monotonic_ns"])
            if age < 0:
                raise PacketAdapterError("node pose timestamp is in the future")
            item["freshness_age_ns"] = age
            item["stale"] = age > max_pose_age_ns
            nodes.append(item)
        jammers: List[Mapping[str, Any]] = []
        for source in self.jammers:
            item = copy.deepcopy(source)
            age = now_ns - int(item["pose_monotonic_ns"])
            if age < 0:
                raise PacketAdapterError("jammer pose timestamp is in the future")
            item["freshness_age_ns"] = age
            item["stale"] = age > max_pose_age_ns
            jammers.append(item)
        return nodes, jammers


@dataclass(frozen=True)
class PacketAdapterConfig:
    identity: ProtocolIdentity
    phase_id: str
    sender_id: str
    provider_sender_id: str
    clock_domain: str
    query_deadline_ns: int
    mapping_seed: int
    source_frame: str
    transform_version: str
    radio_assumptions: Mapping[str, Any]
    antenna_assumptions: Mapping[str, Any]
    material_assumptions: Mapping[str, Any]
    mapping_version: str
    fault_injection_enabled: bool = False
    max_fault_pending_per_cell: int = 2
    query_period_ns: int = 1_000_000_000
    global_query_spacing_ns: int = 0


class PacketSionnaAdapter:
    """Pure event-loop core; ``run_once`` is bounded and contains no waits."""

    def __init__(
        self,
        config: PacketAdapterConfig,
        poses: PoseSnapshot,
        transport: QueryTransport,
        state_writer: AppliedStateIPCWriter,
        audit_path: Path,
        *,
        effects: Optional[PacketEffectsPolicy] = None,
        limits: Optional[ProtocolLimits] = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.config = config
        self.poses = poses
        self._poses_lock = threading.Lock()
        self.transport = transport
        self.state_writer = state_writer
        self.effects = effects or PacketEffectsPolicy.load()
        self.limits = limits or ProtocolLimits()
        self._clock_ns = clock_ns
        self._manager = DirectedLinkStateManager(
            config.identity,
            config.clock_domain,
            limits=self.limits,
            provider_sender_id=config.provider_sender_id,
        )
        self._audit = AppendOnlyJsonl(audit_path)
        self._audit_sequence = 0
        self._query_ordinal_by_cell: Dict[Tuple[str, str], int] = {}
        self._pending_by_cell: Dict[Tuple[str, str], set[str]] = {}
        self._pending_context: Dict[str, Mapping[str, Any]] = {}
        self._last_packet_by_cell: Dict[Tuple[str, str], Mapping[str, Any]] = {}
        self._next_query_due_by_cell: Dict[Tuple[str, str], int] = {}
        self._next_global_query_ns = 0
        if not 1 <= self.config.query_period_ns <= 60_000_000_000:
            raise PacketAdapterError("query_period_ns must be in 1..60000000000")
        if not 0 <= self.config.global_query_spacing_ns <= self.config.query_period_ns:
            raise PacketAdapterError(
                "global_query_spacing_ns must be in 0..query_period_ns"
            )

    def update_poses(self, poses: PoseSnapshot) -> None:
        """Atomically replace the live ROS/Gazebo pose snapshot.

        Query construction copies one complete snapshot while holding the lock;
        provider input can therefore never mix entities from two tracker ticks.
        """

        if not isinstance(poses, PoseSnapshot):
            raise PacketAdapterError("poses must be a PoseSnapshot")
        with self._poses_lock:
            self.poses = poses

    def process_packet_event(
        self,
        event: Mapping[str, Any],
        *,
        _fault_parallel_query: bool = False,
    ) -> Optional[Mapping[str, Any]]:
        packet = _validate_packet_event(event)
        if packet["event"] != "ingress":
            return None
        link = str(packet["directed_link"])
        traffic_class = str(packet["traffic_class"])
        if not CELL_RE.fullmatch(link) or traffic_class not in TRAFFIC_CLASSES:
            return None
        cell = (link, traffic_class)
        self._last_packet_by_cell[cell] = copy.deepcopy(packet)
        now = self._clock_ns()
        with self._poses_lock:
            pose_snapshot = self.poses
        nodes, jammers = pose_snapshot.refreshed(now, self.limits.max_pose_age_ns)
        if any(bool(item["stale"]) for item in [*nodes, *jammers]):
            return self._write_unavailable(packet, "pose_stale")
        pending = self._pending_by_cell.get(cell, set())
        if pending and not _fault_parallel_query:
            self._audit_event("query_coalesced", packet, reason="cell_already_pending")
            return None
        if _fault_parallel_query:
            if not self.config.fault_injection_enabled:
                raise PacketAdapterError("fault-parallel query is not enabled")
            if not 2 <= self.config.max_fault_pending_per_cell <= 8:
                raise PacketAdapterError("fault pending bound must be in 2..8")
            if len(pending) >= self.config.max_fault_pending_per_cell:
                return self._write_unavailable(packet, "fault_pending_bound_exceeded")
        if (
            not _fault_parallel_query
            and now < self._next_query_due_by_cell.get(cell, 0)
        ):
            self._audit_event("query_deferred", packet, reason="update_period_not_due")
            return None
        if not _fault_parallel_query and now < self._next_global_query_ns:
            self._audit_event("query_deferred", packet, reason="global_query_slot_not_due")
            return None
        envelope = self.transport.reserve_query_envelope()
        if envelope is None:
            return self._write_unavailable(packet, "transport_not_ready")
        wire_sequence, generation = envelope
        query_ordinal = self._query_ordinal_by_cell.get(cell, 0) + 1
        self._query_ordinal_by_cell[cell] = query_ordinal
        state_seq = pose_snapshot.snapshot_sequence
        query_id = (
            f"{self.config.identity.run_id}.{link.replace('>', '-to-')}."
            f"{traffic_class}.q{query_ordinal}.s{state_seq}"
        )
        query_message = self._build_query(
            packet,
            query_id=query_id,
            node_state_seq=state_seq,
            wire_sequence=wire_sequence,
            generation=generation,
            now_ns=now,
            poses=pose_snapshot,
        )
        self._manager.register_query(query_message)
        if not self.transport.submit_query(query_message):
            return self._write_unavailable(packet, "query_queue_overflow")
        self._next_query_due_by_cell[cell] = now + self.config.query_period_ns
        if not _fault_parallel_query:
            self._next_global_query_ns = now + self.config.global_query_spacing_ns
        self._pending_by_cell.setdefault(cell, set()).add(query_id)
        self._pending_context[query_id] = {
            "packet_event": copy.deepcopy(packet),
            "query_wire_sha256": hashlib.sha256(
                encode_message(query_message)
            ).hexdigest(),
            "directed_link_id": query_message["directed_link_id"],
            "cell": cell,
        }
        self._audit_event(
            "query_submitted",
            packet,
            query_id=query_id,
            decision="fault_parallel" if _fault_parallel_query else "normal",
            query_wire_sha256=self._pending_context[query_id]["query_wire_sha256"],
        )
        return query_message

    def process_fault_exercise_packet_event(
        self, event: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        """Issue one explicitly bounded parallel query for the F-expiry exercise."""

        return self.process_packet_event(event, _fault_parallel_query=True)

    def refresh_due_cells(self, max_cells: int = 30) -> Tuple[Mapping[str, Any], ...]:
        """Submit periodic refreshes from the latest factual ingress per cell.

        The method is non-blocking and bounded.  It never creates a cell from
        configuration alone: every refresh retains lineage to a real ns-3
        ingress event previously observed by this adapter.
        """

        if not 1 <= max_cells <= 30:
            raise PacketAdapterError("max_cells must be in 1..30")
        now = self._clock_ns()
        output: List[Mapping[str, Any]] = []
        for cell in sorted(self._last_packet_by_cell):
            if len(output) >= max_cells:
                break
            if self._pending_by_cell.get(cell):
                continue
            if now < self._next_query_due_by_cell.get(cell, 0):
                continue
            created = self.process_packet_event(self._last_packet_by_cell[cell])
            if created is not None:
                output.append(created)
        return tuple(output)

    def expire_states(
        self, now_monotonic_ns: Optional[int] = None
    ) -> Tuple[Mapping[str, Any], ...]:
        """Publish every one-shot fail-closed transition at absolute expiry."""

        now = self._clock_ns() if now_monotonic_ns is None else now_monotonic_ns
        output: List[Mapping[str, Any]] = []
        for decision in self._manager.expire(now):
            directed_link_id = decision.directed_link_id
            packet: Optional[Mapping[str, Any]] = None
            if directed_link_id is not None:
                for (link, traffic_class), candidate in self._last_packet_by_cell.items():
                    source, destination = link.split(">", 1)
                    if (
                        f"{source}-to-{destination}-{traffic_class}"
                        == directed_link_id
                    ):
                        packet = candidate
                        break
            if packet is None:
                raise PacketAdapterError(
                    "expired applied state has no factual packet-event lineage"
                )
            output.append(self._write_unavailable(packet, "state_expired"))
            self._audit_event(
                "state_expired",
                packet,
                reason=decision.reason,
                query_id=decision.query_id,
                decision=decision.kind,
                result_wire_sha256=decision.wire_sha256,
                applied_state_id=decision.applied_state_id,
            )
        return tuple(output)

    def poll_results(self, max_items: int = 64) -> Tuple[Mapping[str, Any], ...]:
        output: List[Mapping[str, Any]] = []
        for item in self.transport.poll_results(max_items):
            if isinstance(item, ClientFault):
                for context in list(self._pending_context.values()):
                    self._next_query_due_by_cell[tuple(context["cell"])] = self._clock_ns()
                    output.append(
                        self._write_unavailable(context["packet_event"], item.reason)
                    )
                self._pending_by_cell.clear()
                self._pending_context.clear()
                continue
            now = self._clock_ns()
            decision = self._manager.ingest_result_wire(item, now)
            self._audit_event(
                "result_received",
                {},
                query_id=decision.query_id,
                reason=decision.reason,
                decision=decision.kind,
                result_wire_sha256=decision.wire_sha256,
                adapter_received_monotonic_ns=now,
            )
            query_id = decision.query_id
            if query_id is None or query_id not in self._pending_context:
                self._audit_event(
                    "unmatched_result",
                    {},
                    reason=decision.reason,
                    query_id=query_id,
                    decision=decision.kind,
                    result_wire_sha256=decision.wire_sha256,
                    adapter_received_monotonic_ns=now,
                )
                if decision.kind in {"duplicate", "superseded"}:
                    # The original context is intentionally gone after an
                    # apply/discard.  Still emit the explicit rejection event
                    # required by F-expiry, bound to the unchanged result
                    # bytes and manager decision.
                    self._audit_event(
                        "result_discarded",
                        {},
                        reason=decision.reason,
                        query_id=query_id,
                        decision=decision.kind,
                        result_wire_sha256=decision.wire_sha256,
                        adapter_received_monotonic_ns=now,
                    )
                continue
            context = self._pending_context.pop(query_id)
            cell = tuple(context["cell"])
            pending = self._pending_by_cell.get(cell)
            if pending is not None:
                pending.discard(query_id)
                if not pending:
                    self._pending_by_cell.pop(cell, None)
            if decision.kind != "pending":
                if decision.kind in {"duplicate", "superseded"}:
                    self._audit_event(
                        "result_discarded",
                        context["packet_event"],
                        reason=decision.reason,
                        query_id=query_id,
                        decision=decision.kind,
                        result_wire_sha256=decision.wire_sha256,
                        adapter_received_monotonic_ns=now,
                    )
                    continue
                self._next_query_due_by_cell[cell] = now
                output.append(
                    self._write_unavailable(
                        context["packet_event"],
                        f"result_{decision.kind}:{decision.reason}",
                    )
                )
                continue
            state_id = f"applied-{query_id}-{decision.wire_sha256[:12]}"
            applied = self._manager.apply_latest(
                context["directed_link_id"], now, state_id
            )
            if applied.kind != "applied":
                output.append(
                    self._write_unavailable(
                        context["packet_event"],
                        f"apply_{applied.kind}:{applied.reason}",
                    )
                )
                continue
            state = self._manager.state_for_packet(context["directed_link_id"], now)
            if state is None:
                output.append(
                    self._write_unavailable(context["packet_event"], "state_not_fresh")
                )
                continue
            effects = self.effects.map_physical(state.physical, str(cell[1]))
            packet_hash = _packet_causal_hash(context["packet_event"])
            sample, delivered = packet_delivery_decision(
                packet_causal_sha256=packet_hash,
                applied_state_id=state.applied_state_id,
                mapping_seed=self.config.mapping_seed,
                loss_probability=effects.loss_probability,
            )
            delivered = delivered and effects.service_rate_bps > 0
            payload = {
                "availability": "fresh",
                "unavailable_reason": None,
                "run_id": self.config.identity.run_id,
                "profile": self.config.identity.profile,
                "phase_id": self.config.phase_id,
                "directed_link": cell[0],
                "traffic_class": cell[1],
                "source_packet_event_epoch": context["packet_event"]["event_epoch"],
                "source_packet_event_sequence": context["packet_event"][
                    "event_sequence"
                ],
                "source_packet_uid": context["packet_event"]["packet_uid"],
                "source_packet_causal_sha256": packet_hash,
                "query_id": state.query_id,
                "node_state_seq": state.node_state_seq,
                "query_wire_sha256": context["query_wire_sha256"],
                "result_wire_sha256": state.wire_sha256,
                "applied_state_id": state.applied_state_id,
                "validity_start_monotonic_ns": state.validity_start_monotonic_ns,
                "expires_monotonic_ns": state.expires_monotonic_ns,
                "adapter_applied_monotonic_ns": state.applied_monotonic_ns,
                "physical": copy.deepcopy(state.physical),
                "effects": {
                    "mapping_version": effects.mapping_version,
                    "mapping_seed": self.config.mapping_seed,
                    "propagation_delay_ns": effects.propagation_delay_ns,
                    "loss_probability": effects.loss_probability,
                    "service_rate_bps": effects.service_rate_bps,
                    "reference_loss_sample": sample,
                    "reference_delivery": "deliver" if delivered else "drop",
                    "intervention": "natural",
                },
            }
            record = self.state_writer.write(payload)
            self._audit_event(
                "result_applied",
                context["packet_event"],
                query_id=query_id,
                decision="applied",
                result_wire_sha256=state.wire_sha256,
                applied_state_id=state.applied_state_id,
                adapter_received_monotonic_ns=now,
                adapter_applied_monotonic_ns=state.applied_monotonic_ns,
                validity_start_monotonic_ns=state.validity_start_monotonic_ns,
                expires_monotonic_ns=state.expires_monotonic_ns,
            )
            output.append(record)
        return tuple(output)

    def run_once(
        self,
        tailer: PacketEventTailer,
        *,
        max_packet_events: int = 64,
        max_results: int = 64,
        fault_parallel_cells: Optional[MutableSet[Tuple[str, str]]] = None,
    ) -> Tuple[Mapping[str, Any], ...]:
        """Advance the adapter once without waiting on provider I/O.

        ``fault_parallel_cells`` is used only by the predeclared F-expiry
        exercise.  The first factual ingress for an armed cell submits the
        normal query (or observes the existing held query) and then exactly one
        bounded parallel query.  Configuration alone can never consume an arm.
        """

        output = list(self.expire_states())
        output.extend(self.poll_results(max_results))
        for event in tailer.poll(max_packet_events):
            created = self.process_packet_event(event)
            if created is not None and created.get("schema") == STATE_IPC_SCHEMA:
                output.append(created)
            if fault_parallel_cells:
                link = event.get("directed_link")
                traffic_class = event.get("traffic_class")
                cell = (str(link), str(traffic_class))
                if (
                    event.get("event") == "ingress"
                    and CELL_RE.fullmatch(cell[0]) is not None
                    and cell[1] in TRAFFIC_CLASSES
                    and cell in fault_parallel_cells
                ):
                    # Consume before submission so any exception is fail-closed
                    # and cannot silently retry on a later, unrelated packet.
                    fault_parallel_cells.remove(cell)
                    fault_created = self.process_fault_exercise_packet_event(event)
                    if (
                        fault_created is not None
                        and fault_created.get("schema") == STATE_IPC_SCHEMA
                    ):
                        output.append(fault_created)
        for created in self.refresh_due_cells():
            if created.get("schema") == STATE_IPC_SCHEMA:
                output.append(created)
        return tuple(output)

    def _build_query(
        self,
        packet: Mapping[str, Any],
        *,
        query_id: str,
        node_state_seq: int,
        wire_sequence: int,
        generation: int,
        now_ns: int,
        poses: PoseSnapshot,
    ) -> Mapping[str, Any]:
        nodes, jammers = poses.refreshed(now_ns, self.limits.max_pose_age_ns)
        source, destination = str(packet["directed_link"]).split(">", 1)
        directed_link_id = f"{source}-to-{destination}-{packet['traffic_class']}"
        identity = self.config.identity
        message = {
            "schema_version": 1,
            "message_type": "query",
            "wire_sequence": wire_sequence,
            "sender_id": self.config.sender_id,
            "run_id": identity.run_id,
            "profile": identity.profile,
            "phase_id": self.config.phase_id,
            "contract_hash": identity.contract_hash,
            "config_hash": identity.config_hash,
            "bundle_id": identity.bundle_id,
            "reconnect_generation": generation,
            "sender_clock_domain": self.config.clock_domain,
            "emitted_monotonic_ns": now_ns,
            "query_id": query_id,
            "node_state_seq": node_state_seq,
            "node_state_sha256": poses.snapshot_sha256,
            "node_state_snapshot_monotonic_ns": poses.snapshot_monotonic_ns,
            "directed_link_id": directed_link_id,
            "deadline_monotonic_ns": now_ns + self.config.query_deadline_ns,
            "traffic_class": packet["traffic_class"],
            "tx_node_id": source,
            "rx_node_id": destination,
            "source_pose_monotonic_ns": max(
                [int(item["pose_monotonic_ns"]) for item in nodes + jammers] or [now_ns]
            ),
            "source_frame": self.config.source_frame,
            "transform_version": self.config.transform_version,
            "request_generated_monotonic_ns": now_ns,
            "request_sent_monotonic_ns": now_ns,
            "nodes": nodes,
            "jammers": jammers,
            "radio_assumptions": copy.deepcopy(self.config.radio_assumptions),
            "antenna_assumptions": copy.deepcopy(self.config.antenna_assumptions),
            "material_assumptions": copy.deepcopy(self.config.material_assumptions),
            "mapping_version": self.config.mapping_version,
            "provider_seed": self.config.mapping_seed,
        }
        validate_message(message)
        return message

    def _write_unavailable(
        self, packet: Mapping[str, Any], reason: str
    ) -> Mapping[str, Any]:
        record = self.state_writer.write(
            {
                "availability": "unavailable",
                "unavailable_reason": reason,
                "run_id": self.config.identity.run_id,
                "profile": self.config.identity.profile,
                "phase_id": self.config.phase_id,
                "directed_link": packet.get("directed_link"),
                "traffic_class": packet.get("traffic_class"),
                "source_packet_event_epoch": packet.get("event_epoch"),
                "source_packet_event_sequence": packet.get("event_sequence"),
                "source_packet_uid": packet.get("packet_uid"),
                "source_packet_causal_sha256": _packet_causal_hash(packet)
                if packet
                else None,
                "query_id": None,
                "node_state_seq": None,
                "query_wire_sha256": None,
                "result_wire_sha256": None,
                "applied_state_id": None,
                "validity_start_monotonic_ns": None,
                "expires_monotonic_ns": None,
                "adapter_applied_monotonic_ns": self._clock_ns(),
                "physical": None,
                "effects": None,
            }
        )
        self._audit_event("state_unavailable", packet, reason=reason)
        return record

    def _audit_event(
        self,
        event_name: str,
        packet_event: Mapping[str, Any],
        *,
        reason: Optional[str] = None,
        query_id: Optional[str] = None,
        decision: Optional[str] = None,
        query_wire_sha256: Optional[str] = None,
        result_wire_sha256: Optional[str] = None,
        applied_state_id: Optional[str] = None,
        adapter_received_monotonic_ns: Optional[int] = None,
        adapter_applied_monotonic_ns: Optional[int] = None,
        validity_start_monotonic_ns: Optional[int] = None,
        expires_monotonic_ns: Optional[int] = None,
    ) -> None:
        self._audit_sequence += 1
        self._audit.append(
            {
                "schema": ADAPTER_AUDIT_SCHEMA,
                "audit_sequence": self._audit_sequence,
                "monotonic_ns": self._clock_ns(),
                "adapter_clock_domain": self.config.clock_domain,
                "event": event_name,
                "packet_event_sequence": packet_event.get("event_sequence"),
                "directed_link": packet_event.get("directed_link"),
                "traffic_class": packet_event.get("traffic_class"),
                "query_id": query_id,
                "reason": reason,
                "decision": decision,
                "query_wire_sha256": query_wire_sha256,
                "result_wire_sha256": result_wire_sha256,
                "applied_state_id": applied_state_id,
                "adapter_received_monotonic_ns": adapter_received_monotonic_ns,
                "adapter_applied_monotonic_ns": adapter_applied_monotonic_ns,
                "validity_start_monotonic_ns": validity_start_monotonic_ns,
                "expires_monotonic_ns": expires_monotonic_ns,
            }
        )


def _validate_packet_event(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PacketAdapterError("packet event must be an object")
    required = {
        "schema",
        "event_epoch",
        "event_sequence",
        "sim_time_ns",
        "event",
        "packet_wire_hash",
        "packet_uid",
        "traffic_class",
        "directed_link",
        "transport_protocol",
        "transport_payload_sha256",
    }
    missing = required - set(value)
    if missing:
        raise PacketAdapterError(f"packet event missing fields: {sorted(missing)}")
    if value["schema"] != PACKET_EVENT_SCHEMA:
        raise PacketAdapterError("packet event schema mismatch")
    for key in ("event_epoch", "event_sequence", "sim_time_ns", "packet_uid"):
        _integer(value[key], f"packet_event.{key}", 0)
    _sha256(value["packet_wire_hash"], "packet_event.packet_wire_hash")
    if value["transport_payload_sha256"] is not None:
        _sha256(
            value["transport_payload_sha256"], "packet_event.transport_payload_sha256"
        )
    return value


def _packet_causal_hash(event: Mapping[str, Any]) -> str:
    transport_hash = event.get("transport_payload_sha256")
    if isinstance(transport_hash, str) and SHA256_RE.fullmatch(transport_hash):
        return transport_hash
    wire_hash = event.get("packet_wire_hash")
    return _sha256(wire_hash, "packet_event.packet_wire_hash")


def _unique_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PacketAdapterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_keys(value: Any, path: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PacketAdapterError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        raise PacketAdapterError(
            f"{path} keys mismatch: missing={sorted(expected - actual)} unknown={sorted(actual - expected)}"
        )
    return value


def _integer(value: Any, path: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PacketAdapterError(f"{path} must be integer >= {minimum}")
    return value


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PacketAdapterError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise PacketAdapterError(f"{path} must be finite")
    return result


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PacketAdapterError(f"{path} must be lowercase SHA-256")
    return value


def _safe_id(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or not SAFE_ID_RE.fullmatch(value)
        or len(value) > 128
    ):
        raise PacketAdapterError(f"{path} must be a safe ID")
    return value


__all__ = [
    "AdapterClientConfig",
    "AppliedStateIPCWriter",
    "ClientFault",
    "MappedEffects",
    "PacketAdapterConfig",
    "PacketAdapterError",
    "PacketEffectsPolicy",
    "PacketEventTailer",
    "PacketSionnaAdapter",
    "PoseSnapshot",
    "SionnaAsyncTCPClient",
    "SupervisedResultFaultInjector",
    "deterministic_loss_sample",
    "packet_delivery_decision",
]
