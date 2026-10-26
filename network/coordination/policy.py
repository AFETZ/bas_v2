"""Small, trusted-process lease policy for one Linux host-monotonic clock.

This module does not route packets, synthesize MAVLink responses, or prove flight.
The issuer records leases when issued, including messages never acknowledged.
New leaders wait for the maximum previously issued expiry. Endpoint admission
must happen immediately before the actual UART write, after any queueing.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import math
from typing import Any, Callable


MAX_DATAGRAM_BYTES = 1400
MAX_PAYLOAD_BYTES = 280  # one complete maximum-sized signed MAVLink2 frame
UAV_IDS = frozenset(range(1, 6))


class ProtocolError(ValueError):
    """Malformed or inadmissible protocol input."""


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolError(f"invalid {field}")
    return value


def _uav(value: Any, field: str) -> int:
    if _integer(value, field, 1) not in UAV_IDS:
        raise ProtocolError(f"invalid {field}")
    return value


def _run(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 96:
        raise ProtocolError("invalid run_id")
    return value


@dataclasses.dataclass(frozen=True)
class Grant:
    run_id: str
    epoch: int
    leader_id: int
    grant_sequence: int
    issued_ns: int
    valid_from_ns: int
    valid_until_ns: int

    def __post_init__(self) -> None:
        _run(self.run_id)
        _integer(self.epoch, "epoch", 1)
        _uav(self.leader_id, "leader_id")
        _integer(self.grant_sequence, "grant_sequence", 1)
        _integer(self.issued_ns, "issued_ns")
        _integer(self.valid_from_ns, "valid_from_ns")
        _integer(self.valid_until_ns, "valid_until_ns")
        if not self.issued_ns <= self.valid_from_ns < self.valid_until_ns:
            raise ProtocolError("invalid grant time interval")


@dataclasses.dataclass(frozen=True)
class Command:
    run_id: str
    epoch: int
    leader_id: int
    grant_sequence: int
    sequence: int
    destination_id: int
    created_ns: int
    valid_from_ns: int
    valid_until_ns: int
    payload: bytes
    target_position_m: tuple[float, float, float] | None = None
    target_frame: str | None = None

    def __post_init__(self) -> None:
        _run(self.run_id)
        _integer(self.epoch, "epoch", 1)
        _uav(self.leader_id, "leader_id")
        _uav(self.destination_id, "destination_id")
        for field in ("grant_sequence", "sequence"):
            _integer(getattr(self, field), field, 1)
        for field in ("created_ns", "valid_from_ns", "valid_until_ns"):
            _integer(getattr(self, field), field)
        if not self.valid_from_ns < self.valid_until_ns or self.created_ns >= self.valid_until_ns:
            raise ProtocolError("invalid command time interval")
        if not isinstance(self.payload, bytes) or not 1 <= len(self.payload) <= MAX_PAYLOAD_BYTES:
            raise ProtocolError("invalid command payload")
        if self.target_position_m is not None:
            if len(self.target_position_m) != 3 or not all(
                type(x) in (int, float) and math.isfinite(x) for x in self.target_position_m
            ):
                raise ProtocolError("invalid target_position_m")
            if self.target_frame not in ("ENU", "NED", "MAVLINK_RELATIVE_ALT", "AMSL"):
                raise ProtocolError("target_frame required for target_position_m")

    @property
    def command_id(self) -> str:
        return f"{self.epoch}:{self.destination_id}:{self.sequence}"


def encode(kind: str, sender_id: int, body: Any) -> bytes:
    """Wrap opaque bytes without modifying their MAVLink content."""
    if kind not in ("grant", "command", "telemetry", "heartbeat", "probe"):
        raise ProtocolError("unknown packet kind")
    if type(sender_id) is not int or sender_id not in {0, *UAV_IDS}:
        raise ProtocolError("invalid sender_id")
    value = dataclasses.asdict(body) if dataclasses.is_dataclass(body) else dict(body)
    if isinstance(value.get("payload"), bytes):
        value["payload"] = base64.b64encode(value["payload"]).decode("ascii")
    raw = json.dumps({"v": 1, "kind": kind, "sender_id": sender_id, "body": value},
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_DATAGRAM_BYTES:
        raise ProtocolError("coordination datagram exceeds configured MTU budget")
    return raw


def decode(raw: bytes) -> tuple[str, int, Grant | Command | dict[str, Any]]:
    if len(raw) > MAX_DATAGRAM_BYTES:
        raise ProtocolError("oversized datagram")
    try:
        value = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if set(value) != {"v", "kind", "sender_id", "body"} or type(value["v"]) is not int or value["v"] != 1:
            raise ProtocolError("unknown protocol envelope")
        kind, sender, body = value["kind"], value["sender_id"], value["body"]
        if type(sender) is not int or sender not in {0, *UAV_IDS} or not isinstance(body, dict):
            raise ProtocolError("invalid sender/body")
        if kind == "grant":
            if sender != 0:
                raise ProtocolError("only GCS issues grants")
            return kind, sender, Grant(**body)
        if kind == "command":
            body["payload"] = base64.b64decode(body["payload"], validate=True)
            if body.get("target_position_m") is not None:
                body["target_position_m"] = tuple(body["target_position_m"])
            return kind, sender, Command(**body)
        if kind in ("telemetry", "heartbeat", "probe"):
            return kind, sender, body
        raise ProtocolError("unknown packet kind")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ProtocolError(str(exc)) from exc


class Authority:
    """The only issuer. State belongs to this run; restart requires a new run_id."""

    def __init__(self, run_id: str, *, skew_margin_ns: int = 0) -> None:
        self.run_id = _run(run_id)
        self.skew_margin_ns = _integer(skew_margin_ns, "skew_margin_ns")
        self.epoch = 0
        self.leader_id: int | None = None
        self.grant_sequence = 0
        self.max_issued_expiry_ns = 0
        self.active_from_ns = 0
        self.last_issue_ns = -1

    def issue(self, leader_id: int, now_ns: int, lease_ns: int) -> Grant:
        _uav(leader_id, "leader_id")
        _integer(now_ns, "now_ns")
        _integer(lease_ns, "lease_ns", 1)
        if now_ns < self.last_issue_ns:
            raise ProtocolError("issuer clock moved backwards")
        if leader_id != self.leader_id:
            self.active_from_ns = max(now_ns, self.max_issued_expiry_ns + self.skew_margin_ns) if self.epoch else now_ns
            self.epoch += 1
            self.leader_id = leader_id
        self.grant_sequence += 1
        valid_from = max(now_ns, self.active_from_ns)
        grant = Grant(self.run_id, self.epoch, leader_id, self.grant_sequence, now_ns,
                      valid_from, valid_from + lease_ns)
        # Update on issue, regardless of transport delivery or acknowledgement.
        self.max_issued_expiry_ns = max(self.max_issued_expiry_ns, grant.valid_until_ns)
        self.last_issue_ns = now_ns
        return grant


class AdmissionGate:
    """Final endpoint fence. No outcome is inferred from passing this check."""

    def __init__(self, run_id: str, uav_id: int, *, now_ns: Callable[[], int],
                 hold: Callable[[], None], emit: Callable[..., None] | None = None) -> None:
        self.run_id, self.uav_id = _run(run_id), _uav(uav_id, "uav_id")
        self.now_ns, self.hold = now_ns, hold
        self.emit = emit or (lambda *args, **kwargs: None)
        self.grants: dict[int, Grant] = {}
        self.highest_epoch = 0
        self.highest_grant_sequence = 0
        self.last_sequence: dict[int, int] = {}
        self.hold_required = False
        self.ever_active = False

    def receive_grant(self, grant: Grant, *, sender_id: int) -> bool:
        now = self.now_ns()
        if sender_id != 0 or grant.run_id != self.run_id:
            raise ProtocolError("untrusted issuer or foreign run")
        if grant.issued_ns > now:
            raise ProtocolError("grant issued in future")
        if grant.valid_until_ns <= now:
            raise ProtocolError("expired grant")
        if grant.epoch < self.highest_epoch:
            raise ProtocolError("old grant epoch")
        existing = self.grants.get(grant.grant_sequence)
        if existing == grant:
            return False  # byte-identical replay never extends a lease
        if grant.grant_sequence <= self.highest_grant_sequence:
            raise ProtocolError("old or conflicting grant sequence")
        for old in self.grants.values():
            if old.epoch == grant.epoch and old.leader_id != grant.leader_id:
                raise ProtocolError("leader changed inside epoch")
            if old.epoch < grant.epoch and old.valid_until_ns > grant.valid_from_ns:
                raise ProtocolError("overlapping leader leases")
        self.grants[grant.grant_sequence] = grant
        self.highest_epoch = grant.epoch
        self.highest_grant_sequence = grant.grant_sequence
        self.emit("grant_received", epoch=grant.epoch, leader_id=grant.leader_id,
                  valid_from_ns=grant.valid_from_ns, valid_until_ns=grant.valid_until_ns)
        return True

    def active_grant(self) -> Grant | None:
        now = self.now_ns()
        active = [g for g in self.grants.values() if g.valid_from_ns <= now < g.valid_until_ns]
        if not active:
            return None
        return max(active, key=lambda g: (g.epoch, g.grant_sequence))

    def check(self, command: Command, *, sender_id: int, reserve: bool = False) -> None:
        """Call before EVERY write, including a partial-write continuation."""
        now = self.now_ns()
        if command.run_id != self.run_id or command.destination_id != self.uav_id:
            raise ProtocolError("foreign run or destination")
        if sender_id != command.leader_id:
            raise ProtocolError("command did not arrive from its coordinator")
        grant = self.grants.get(command.grant_sequence)
        active = self.active_grant()
        if grant is None or active is None:
            raise ProtocolError("no active received grant")
        if command.epoch != grant.epoch or command.leader_id != grant.leader_id or active.epoch != command.epoch:
            raise ProtocolError("stale epoch or wrong coordinator")
        if command.valid_from_ns != grant.valid_from_ns or command.valid_until_ns != grant.valid_until_ns:
            raise ProtocolError("command altered grant interval")
        if not command.valid_from_ns <= now < command.valid_until_ns or command.created_ns > now:
            raise ProtocolError("command not currently valid")
        if command.sequence <= self.last_sequence.get(command.epoch, 0):
            raise ProtocolError("duplicate or reordered command")
        if reserve:
            self.last_sequence[command.epoch] = command.sequence
            self.ever_active = True
            self.hold_required = True

    def tick(self) -> None:
        """Request native HOLD on expiry; the caller must observe actual behavior."""
        active = self.active_grant()
        if active is not None:
            self.ever_active = True
            self.hold_required = True
        elif self.ever_active and self.hold_required:
            self.hold()  # no success event if native request raises
            self.hold_required = False
            self.emit("hold_requested", reason="lease_expired")


def make_command(grant: Grant, *, destination_id: int, sequence: int, now_ns: int,
                 payload: bytes, target_position_m: tuple[float, float, float] | None = None,
                 target_frame: str | None = None) -> Command:
    return Command(grant.run_id, grant.epoch, grant.leader_id, grant.grant_sequence,
                   sequence, destination_id, now_ns, grant.valid_from_ns,
                   grant.valid_until_ns, payload, target_position_m, target_frame)


class UARTAdmission:
    """Bounded pending records with fencing repeated at the actual write boundary.

    A partial record that expires aborts the run. Its previously written prefix
    cannot be undone or counted as a successful command or guaranteed HOLD.
    """

    def __init__(self, gate: AdmissionGate, write: Callable[[bytes], int], *,
                 emit: Callable[..., None] | None = None, max_pending: int = 64) -> None:
        from collections import deque
        self.gate, self.write = gate, write
        self.emit = emit or (lambda *args, **kwargs: None)
        self.queue: Any = deque()
        self.max_pending = max_pending
        self.pending_ids: set[str] = set()

    def enqueue(self, command: Command, *, sender_id: int) -> None:
        self.gate.check(command, sender_id=sender_id)
        if command.command_id in self.pending_ids:
            raise ProtocolError("duplicate pending command")
        if len(self.queue) >= self.max_pending:
            raise ProtocolError("UART command queue full")
        self.queue.append([command, sender_id, 0])
        self.pending_ids.add(command.command_id)

    def drain_one(self) -> bool:
        if not self.queue:
            return False
        command, sender, offset = self.queue[0]
        try:
            self.gate.check(command, sender_id=sender)
        except ProtocolError:
            self.queue.popleft()
            self.pending_ids.discard(command.command_id)
            if offset:
                raise RuntimeError("lease/epoch invalidated a partially written UART command")
            self.emit("command_rejected", command=command, reason="expired_or_stale_at_uart")
            return True
        try:
            written = self.write(command.payload[offset:])
        except BlockingIOError:
            return False
        if type(written) is not int or written <= 0 or written > len(command.payload) - offset:
            raise RuntimeError("invalid UART write result")
        offset += written
        if offset < len(command.payload):
            self.queue[0][2] = offset
            return True
        # The final fragment was checked immediately before this synchronous write.
        self.gate.check(command, sender_id=sender, reserve=True)
        self.queue.popleft()
        self.pending_ids.discard(command.command_id)
        self.emit("uart_admitted", command=command, bytes=offset)
        return True
