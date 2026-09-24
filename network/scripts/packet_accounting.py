#!/usr/bin/env python3
"""Logical packet accounting kept separate from ns-3 event accounting."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


class PacketAccountingError(ValueError):
    """Attempt/delivery input is ambiguous or violates the packet invariant."""


OUTCOME_STATUSES = (
    "delivered",
    "dropped_at_ingress",
    "dropped_in_medium",
    "expired_at_drain",
    "pending",
)

_INGRESS_DROP_PREFIXES = (
    "ingress_token_bucket",
    "admission_token_bucket",
    "token_bucket",
    "ingress_shaping",
    "shaping_",
    "shaper_",
    "ingress_deadline",
    "admission_deadline",
    "deadline_drop_ingress",
)


def _drop_reason(event: Mapping[str, Any]) -> str:
    value = event.get("drop_reason", event.get("reason", ""))
    return str(value or "unspecified_drop")


def _is_ingress_drop(event: Mapping[str, Any], reason: str) -> bool:
    stage = str(event.get("drop_stage") or "").lower().replace("-", "_")
    terminal_status = str(event.get("terminal_status") or "").lower()
    normalized = reason.lower().replace("-", "_")
    return (
        stage in {"admission", "ingress"}
        or terminal_status == "dropped_at_ingress"
        or normalized.startswith(_INGRESS_DROP_PREFIXES)
    )


def _drop_kind(event: Mapping[str, Any]) -> str | None:
    if event.get("event") not in {"drop", "admission_drop"}:
        return None
    reason = _drop_reason(event)
    if _is_ingress_drop(event, reason):
        return "ingress"
    if reason.startswith(("queue_", "aggregate_queue", "deadline_drop")):
        return "queue"
    return "phy"


def _collect_observations(
    attempts: Iterable[Mapping[str, Any]],
    deliveries: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[str, Mapping[str, Any]],
    Counter[str],
    dict[str, dict[str, Any]],
    Counter[str],
    int,
]:
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

    observations: dict[str, dict[str, Any]] = {
        packet_id: {"drops": [], "backoff_events": 0, "retry_events": 0}
        for packet_id in attempt_map
    }
    event_counts: Counter[str] = Counter()
    for event in events:
        explicit_packet_id = event.get("packet_id")
        if isinstance(explicit_packet_id, str) and explicit_packet_id in attempt_map:
            matching_packets: Iterable[str] = (explicit_packet_id,)
        else:
            digest = event.get("transport_payload_sha256")
            matching_packets = (
                hashes_to_packets.get(digest, ()) if isinstance(digest, str) else ()
            )
        matching_packets = tuple(matching_packets)
        # Medium events for telemetry and other background datagrams remain
        # part of channel/load metrics, but are not logical test-packet events.
        if not matching_packets:
            continue
        event_name = event.get("event")
        if event_name == "backoff":
            event_counts["backoff"] += 1
            # One CSMA backoff trace is also one factual transmission retry.
            event_counts["retry"] += 1
            for packet_id in matching_packets:
                observations[packet_id]["backoff_events"] += 1
                observations[packet_id]["retry_events"] += 1
        elif event_name == "retry":
            event_counts["retry"] += 1
            for packet_id in matching_packets:
                observations[packet_id]["retry_events"] += 1
        kind = _drop_kind(event)
        if kind:
            event_counts[f"{kind}_drop"] += 1
            reason = _drop_reason(event)
            for packet_id in matching_packets:
                observations[packet_id]["drops"].append(
                    {"kind": kind, "reason": reason}
                )

    return attempt_map, delivery_counts, observations, event_counts, malformed


def _build_outcomes(
    attempt_map: Mapping[str, Mapping[str, Any]],
    delivery_counts: Counter[str],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    finalize_pending: bool,
) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for packet_id in attempt_map:
        delivery_count = int(delivery_counts[packet_id])
        packet_observations = observations[packet_id]
        drops = list(packet_observations["drops"])
        drop_reason: str | None = None
        if delivery_count:
            status = "delivered"
        elif drops:
            first_drop = drops[0]
            status = (
                "dropped_at_ingress"
                if first_drop["kind"] == "ingress"
                else "dropped_in_medium"
            )
            drop_reason = str(first_drop["reason"])
        elif finalize_pending:
            status = "expired_at_drain"
            drop_reason = "drain_expired"
        else:
            status = "pending"
        outcomes[packet_id] = {
            "packet_id": packet_id,
            "status": status,
            "terminal": status != "pending",
            "drop_reason": drop_reason,
            "delivery_count": delivery_count,
            "duplicate_deliveries": max(0, delivery_count - 1),
            "drop_event_count": len(drops),
            "backoff_events": int(packet_observations["backoff_events"]),
            "retry_events": int(packet_observations["retry_events"]),
        }
    return outcomes


def terminal_packet_outcomes(
    attempts: Iterable[Mapping[str, Any]],
    deliveries: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    *,
    finalize_pending: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return one outcome record for every valid logical packet attempt.

    Delivery is authoritative even if earlier backoff or drop observations
    exist.  Without finalization, an unresolved attempt has status ``pending``.
    At the end of a bounded drain, callers may set ``finalize_pending`` to turn
    every unresolved attempt into the terminal ``expired_at_drain`` status.
    """

    attempt_map, delivery_counts, observations, _event_counts, _malformed = (
        _collect_observations(attempts, deliveries, events)
    )
    return _build_outcomes(
        attempt_map,
        delivery_counts,
        observations,
        finalize_pending=finalize_pending,
    )


def account_packets(
    attempts: Iterable[Mapping[str, Any]],
    deliveries: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    *,
    finalize_pending: bool = False,
) -> dict[str, Any]:
    """Account logical attempts exactly once, regardless of fragments/events.

    Attempts must carry a unique ``packet_id`` and may carry one or more
    ``fragment_hashes`` (or one ``transport_payload_sha256``).  Deliveries use
    the same logical ``packet_id``.  A packet delivered after any number of
    backoff/retry/drop-like intermediate events remains delivered, never
    double-counted as dropped.
    """

    attempt_map, delivery_counts, observations, event_counts, malformed = (
        _collect_observations(attempts, deliveries, events)
    )
    outcomes = _build_outcomes(
        attempt_map,
        delivery_counts,
        observations,
        finalize_pending=finalize_pending,
    )
    status_counts = {status: 0 for status in OUTCOME_STATUSES}
    for outcome in outcomes.values():
        status_counts[str(outcome["status"])] += 1

    delivered = status_counts["delivered"]
    dropped_at_ingress = status_counts["dropped_at_ingress"]
    dropped_in_medium = status_counts["dropped_in_medium"]
    expired_at_drain = status_counts["expired_at_drain"]
    dropped = dropped_at_ingress + dropped_in_medium + expired_at_drain
    pending = status_counts["pending"]
    duplicate_deliveries = sum(count - 1 for count in delivery_counts.values())
    result: dict[str, Any] = {
        "packets_attempted": len(attempt_map),
        "packets_delivered_unique": delivered,
        "packets_dropped_unique": dropped,
        "packets_dropped_at_ingress_unique": dropped_at_ingress,
        "packets_dropped_in_medium_unique": dropped_in_medium,
        "packets_expired_at_drain": expired_at_drain,
        "packets_pending": pending,
        "duplicate_deliveries": duplicate_deliveries,
        "ingress_drop_events": event_counts["ingress_drop"],
        "queue_drop_events": event_counts["queue_drop"],
        "phy_drop_events": event_counts["phy_drop"],
        "backoff_events": event_counts["backoff"],
        "retry_events": event_counts["retry"],
        "malformed_packets": malformed,
        "terminal_status_counts": status_counts,
        "all_packets_terminal": pending == 0,
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
    *,
    finalize_pending: bool = False,
) -> dict[str, dict[str, Any]]:
    """Apply logical accounting independently to requested attempt dimensions."""

    result: dict[str, dict[str, Any]] = {}
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
                finalize_pending=finalize_pending,
            )
    return result
