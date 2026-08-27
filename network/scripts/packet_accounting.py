#!/usr/bin/env python3
"""Logical packet accounting kept separate from ns-3 event accounting."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


class PacketAccountingError(ValueError):
    """Attempt/delivery input is ambiguous or violates the packet invariant."""


def _drop_kind(event: Mapping[str, Any]) -> str | None:
    if event.get("event") != "drop":
        return None
    reason = str(event.get("drop_reason") or "")
    if reason.startswith(("queue_", "aggregate_queue", "deadline_drop")):
        return "queue"
    return "phy"


def account_packets(
    attempts: Iterable[Mapping[str, Any]],
    deliveries: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> dict[str, int | bool]:
    """Account logical attempts exactly once, regardless of fragments/events.

    Attempts must carry a unique ``packet_id`` and may carry one or more
    ``fragment_hashes`` (or one ``transport_payload_sha256``).  Deliveries use
    the same logical ``packet_id``.  A packet delivered after any number of
    backoff/retry/drop-like intermediate events remains delivered, never
    double-counted as dropped.
    """

    attempt_map: dict[str, Mapping[str, Any]] = {}
    hashes_to_packets: dict[str, set[str]] = defaultdict(set)
    malformed = 0
    for item in attempts:
        packet_id = item.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id:
            malformed += 1
            continue
        if packet_id in attempt_map:
            raise PacketAccountingError(f"duplicate packet attempt: {packet_id}")
        attempt_map[packet_id] = item
        hashes = item.get("fragment_hashes")
        if hashes is None:
            single = item.get("transport_payload_sha256")
            hashes = [single] if isinstance(single, str) and single else []
        if not isinstance(hashes, list) or not all(
            isinstance(value, str) and value for value in hashes
        ):
            raise PacketAccountingError(f"invalid fragment hashes for {packet_id}")
        for digest in set(hashes):
            hashes_to_packets[digest].add(packet_id)

    delivery_counts: Counter[str] = Counter()
    for item in deliveries:
        if item.get("malformed"):
            malformed += 1
            continue
        packet_id = item.get("packet_id")
        if not isinstance(packet_id, str) or packet_id not in attempt_map:
            malformed += 1
            continue
        delivery_counts[packet_id] += 1

    dropped_candidates: set[str] = set()
    queue_drop_events = 0
    phy_drop_events = 0
    backoff_events = 0
    retry_events = 0
    for event in events:
        event_name = event.get("event")
        if event_name == "backoff":
            backoff_events += 1
            # One CSMA backoff trace is also one factual transmission retry.
            retry_events += 1
        elif event_name == "retry":
            retry_events += 1
        kind = _drop_kind(event)
        if kind == "queue":
            queue_drop_events += 1
        elif kind == "phy":
            phy_drop_events += 1
        if kind:
            digest = event.get("transport_payload_sha256")
            if isinstance(digest, str):
                dropped_candidates.update(hashes_to_packets.get(digest, ()))

    delivered = set(delivery_counts)
    dropped = dropped_candidates - delivered
    pending = set(attempt_map) - delivered - dropped
    duplicate_deliveries = sum(count - 1 for count in delivery_counts.values())
    result: dict[str, int | bool] = {
        "packets_attempted": len(attempt_map),
        "packets_delivered_unique": len(delivered),
        "packets_dropped_unique": len(dropped),
        "packets_pending": len(pending),
        "duplicate_deliveries": duplicate_deliveries,
        "queue_drop_events": queue_drop_events,
        "phy_drop_events": phy_drop_events,
        "backoff_events": backoff_events,
        "retry_events": retry_events,
        "malformed_packets": malformed,
    }
    result["packet_invariant_holds"] = result["packets_attempted"] == (
        result["packets_delivered_unique"]
        + result["packets_dropped_unique"]
        + result["packets_pending"]
    )
    if not result["packet_invariant_holds"]:
        raise AssertionError("logical packet accounting invariant failed")
    return result


def group_accounting(
    attempts: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    events: list[dict[str, Any]],
    keys: tuple[str, ...],
) -> dict[str, dict[str, int | bool]]:
    """Apply logical accounting independently to requested attempt dimensions."""

    result: dict[str, dict[str, int | bool]] = {}
    for key in keys:
        values = sorted({str(item.get(key, "unknown")) for item in attempts})
        for value in values:
            selected = [item for item in attempts if str(item.get(key, "unknown")) == value]
            ids = {str(item["packet_id"]) for item in selected if "packet_id" in item}
            relevant_hashes = {
                digest
                for item in selected
                for digest in item.get("fragment_hashes", [])
                if isinstance(digest, str)
            }
            result[f"{key}:{value}"] = account_packets(
                selected,
                [item for item in deliveries if item.get("packet_id") in ids],
                [
                    item
                    for item in events
                    if item.get("transport_payload_sha256") in relevant_hashes
                ],
            )
    return result
