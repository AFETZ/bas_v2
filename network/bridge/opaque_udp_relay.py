#!/usr/bin/env python3
"""Shared M2/M3 byte-opaque UDP relay core.

This module is deliberately protocol-blind.  It does not decode, encode,
buffer, retry, concatenate, split, or otherwise transform an application
datagram.  A successful relay performs exactly one ``sendto`` with the exact
``bytes`` object supplied by the caller and verifies the returned byte count.
Readiness, authorization, evidence, and process-lineage policy belong to the
formal M2/M3 wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


Endpoint = tuple[str, int]
Direction = Literal["tail_to_gcs", "gcs_to_tail"]
Action = Literal["forwarded", "dropped", "held"]


class RelayError(RuntimeError):
    """The byte-opaque relay cannot preserve its fail-closed contract."""


class TailPeerReplaced(RelayError):
    """A strict dynamic tail peer changed after it was learned."""


@dataclass(frozen=True)
class RelayDecision:
    action: Action
    direction: Direction
    reason: str
    source: Endpoint
    destination: Endpoint | None
    byte_count: int


def _endpoint(value: Endpoint, label: str) -> Endpoint:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not value[0]
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
        or not 1 <= value[1] <= 65535
    ):
        raise RelayError(f"{label} is not an exact (host, port) endpoint: {value!r}")
    return value


class ByteOpaqueUdpRelay:
    """One shared, protocol-blind relay implementation for M2 and M3."""

    def __init__(
        self,
        radio_socket: Any,
        tail_socket: Any,
        gcs_peer: Endpoint,
        *,
        tail_peer_host: str,
        strict_tail_peer: bool,
        forwarding_enabled: bool,
        before_forward: Callable[[], None] | None = None,
    ):
        if not isinstance(tail_peer_host, str) or not tail_peer_host:
            raise RelayError("tail_peer_host must be a nonempty string")
        self.radio = radio_socket
        self.tail = tail_socket
        self.gcs_peer = _endpoint(gcs_peer, "gcs_peer")
        self.tail_peer_host = tail_peer_host
        self.strict_tail_peer = strict_tail_peer
        self.authorized = forwarding_enabled
        self.lineage_check = before_forward if before_forward is not None else lambda: None
        self.mavproxy_peer: Endpoint | None = None

    @staticmethod
    def _payload(payload: bytes) -> bytes:
        # Requiring the exact immutable built-in type prevents mutable views or
        # implicit conversions from changing bytes between audit and sendto.
        if type(payload) is not bytes:  # noqa: E721 - exact type is the contract
            raise RelayError("relay payload must be the immutable built-in bytes type")
        if len(payload) > 65507:
            raise RelayError("relay payload exceeds the maximum IPv4 UDP datagram payload")
        return payload

    @staticmethod
    def _send_exact(sock: Any, payload: bytes, destination: Endpoint) -> None:
        sent = sock.sendto(payload, destination)
        if isinstance(sent, bool) or not isinstance(sent, int) or sent != len(payload):
            raise RelayError(
                f"UDP sendto byte count differs from exact payload size: sent={sent!r}, "
                f"expected={len(payload)}"
            )

    def lock_peer(self, peer: Endpoint) -> None:
        peer = _endpoint(peer, "tail_peer")
        if peer[0] != self.tail_peer_host:
            raise RelayError(
                f"tail peer host {peer[0]!r} differs from {self.tail_peer_host!r}"
            )
        if self.mavproxy_peer is not None and peer != self.mavproxy_peer:
            if self.strict_tail_peer:
                raise TailPeerReplaced(
                    f"dynamic MAVProxy peer replacement: {self.mavproxy_peer!r} -> {peer!r}"
                )
        self.mavproxy_peer = peer

    def authorize(self) -> None:
        if self.mavproxy_peer is None:
            raise RelayError("cannot authorize before learning a MAVProxy peer")
        self.lineage_check()
        self.authorized = True

    def relay_tail(self, payload: bytes, peer: Endpoint) -> RelayDecision:
        payload = self._payload(payload)
        peer = _endpoint(peer, "tail_source")
        if peer[0] != self.tail_peer_host:
            return RelayDecision(
                "dropped",
                "tail_to_gcs",
                "unexpected_tail_peer",
                peer,
                None,
                len(payload),
            )
        self.lock_peer(peer)
        if not self.authorized:
            return RelayDecision(
                "held",
                "tail_to_gcs",
                "endpoint_not_authorized",
                peer,
                self.gcs_peer,
                len(payload),
            )
        self.lineage_check()
        self._send_exact(self.radio, payload, self.gcs_peer)
        return RelayDecision(
            "forwarded",
            "tail_to_gcs",
            "exact_payload_relay",
            peer,
            self.gcs_peer,
            len(payload),
        )

    def relay_radio(self, payload: bytes, peer: Endpoint) -> RelayDecision:
        payload = self._payload(payload)
        peer = _endpoint(peer, "radio_source")
        if peer != self.gcs_peer:
            return RelayDecision(
                "dropped",
                "gcs_to_tail",
                "unexpected_gcs_peer",
                peer,
                None,
                len(payload),
            )
        if not self.authorized:
            return RelayDecision(
                "dropped",
                "gcs_to_tail",
                "endpoint_not_authorized",
                peer,
                self.mavproxy_peer,
                len(payload),
            )
        if self.mavproxy_peer is None:
            return RelayDecision(
                "dropped",
                "gcs_to_tail",
                "mavproxy_peer_unknown",
                peer,
                None,
                len(payload),
            )
        self.lineage_check()
        self._send_exact(self.tail, payload, self.mavproxy_peer)
        return RelayDecision(
            "forwarded",
            "gcs_to_tail",
            "exact_payload_relay",
            peer,
            self.mavproxy_peer,
            len(payload),
        )
