"""Unit tests for logical packet/event accounting."""

from __future__ import annotations

import unittest

from network.scripts.packet_accounting import account_packets, terminal_packet_outcomes
from scripts.product.summarize_overload_protection import terminal_ledger_sha256


def attempt(packet_id: str, *hashes: str) -> dict[str, object]:
    return {"packet_id": packet_id, "fragment_hashes": list(hashes)}


class PacketAccountingTests(unittest.TestCase):
    def test_terminal_ledger_digest_is_order_stable_and_status_sensitive(self) -> None:
        first = {
            "p2": {"packet_id": "p2", "status": "dropped_at_ingress", "terminal": True},
            "p1": {"packet_id": "p1", "status": "delivered", "terminal": True},
        }
        reordered = {"p1": first["p1"], "p2": first["p2"]}
        changed = {
            **reordered,
            "p2": {"packet_id": "p2", "status": "dropped_in_medium", "terminal": True},
        }

        self.assertEqual(
            terminal_ledger_sha256(first), terminal_ledger_sha256(reordered)
        )
        self.assertNotEqual(
            terminal_ledger_sha256(first), terminal_ledger_sha256(changed)
        )

    def test_duplicate_delivery_is_not_a_second_delivered_packet(self) -> None:
        result = account_packets(
            [attempt("p1", "h1")],
            [{"packet_id": "p1"}, {"packet_id": "p1"}],
            [],
        )
        self.assertEqual(result["packets_delivered_unique"], 1)
        self.assertEqual(result["duplicate_deliveries"], 1)

    def test_multiple_drop_events_are_one_dropped_packet(self) -> None:
        result = account_packets(
            [attempt("p1", "h1")],
            [],
            [
                {"event": "drop", "drop_reason": "queue_limit_payload", "transport_payload_sha256": "h1"},
                {"event": "drop", "drop_reason": "phy_tx_drop", "transport_payload_sha256": "h1"},
            ],
        )
        self.assertEqual(result["packets_dropped_unique"], 1)
        self.assertEqual(result["queue_drop_events"], 1)
        self.assertEqual(result["phy_drop_events"], 1)

    def test_packet_without_delivery_or_terminal_drop_is_pending(self) -> None:
        result = account_packets([attempt("p1", "h1")], [], [])
        self.assertEqual(result["packets_pending"], 1)
        self.assertTrue(result["packet_invariant_holds"])

    def test_packet_delivered_after_retries_is_delivered_not_dropped(self) -> None:
        result = account_packets(
            [attempt("p1", "h1")],
            [{"packet_id": "p1"}],
            [
                {"event": "backoff", "transport_payload_sha256": "h1"},
                {"event": "retry", "transport_payload_sha256": "h1"},
            ],
        )
        self.assertEqual(result["packets_delivered_unique"], 1)
        self.assertEqual(result["packets_dropped_unique"], 0)
        self.assertEqual(result["backoff_events"], 1)
        self.assertEqual(result["retry_events"], 2)

    def test_fragmented_packet_is_one_logical_attempt(self) -> None:
        result = account_packets(
            [attempt("p1", "fragment-a", "fragment-b", "fragment-c")],
            [],
            [
                {
                    "event": "drop",
                    "drop_reason": "sionna_loss",
                    "transport_payload_sha256": "fragment-b",
                }
            ],
        )
        self.assertEqual(result["packets_attempted"], 1)
        self.assertEqual(result["packets_dropped_unique"], 1)
        self.assertEqual(result["phy_drop_events"], 1)
        self.assertTrue(result["packet_invariant_holds"])

    def test_background_medium_events_are_not_logical_packet_events(self) -> None:
        result = account_packets(
            [attempt("p1", "logical")],
            [{"packet_id": "p1"}],
            [
                {"event": "backoff", "transport_payload_sha256": "background"},
                {
                    "event": "drop",
                    "drop_reason": "queue_limit_payload",
                    "transport_payload_sha256": "background",
                },
            ],
        )
        self.assertEqual(result["backoff_events"], 0)
        self.assertEqual(result["retry_events"], 0)
        self.assertEqual(result["queue_drop_events"], 0)
        self.assertEqual(result["packets_delivered_unique"], 1)

    def test_terminal_outcomes_classify_ingress_medium_and_pending_once(self) -> None:
        attempts = [
            attempt("delivered", "h1"),
            attempt("shaped", "h2"),
            attempt("medium", "h3"),
            attempt("pending", "h4"),
        ]
        deliveries = [{"packet_id": "delivered"}]
        events = [
            {
                "event": "drop",
                "drop_reason": "ingress_token_bucket_payload",
                "transport_payload_sha256": "h2",
            },
            {
                "event": "drop",
                "drop_reason": "sionna_loss",
                "transport_payload_sha256": "h3",
            },
            {
                "event": "drop",
                "drop_reason": "phy_tx_drop",
                "transport_payload_sha256": "h3",
            },
        ]
        outcomes = terminal_packet_outcomes(attempts, deliveries, events)
        accounting = account_packets(attempts, deliveries, events)

        self.assertEqual(len(outcomes), 4)
        self.assertEqual(outcomes["delivered"]["status"], "delivered")
        self.assertEqual(outcomes["shaped"]["status"], "dropped_at_ingress")
        self.assertEqual(outcomes["medium"]["status"], "dropped_in_medium")
        self.assertEqual(outcomes["medium"]["drop_reason"], "sionna_loss")
        self.assertEqual(outcomes["medium"]["drop_event_count"], 2)
        self.assertEqual(outcomes["pending"]["status"], "pending")
        self.assertFalse(outcomes["pending"]["terminal"])
        self.assertEqual(
            accounting["terminal_status_counts"],
            {
                "delivered": 1,
                "dropped_at_ingress": 1,
                "dropped_in_medium": 1,
                "expired_at_drain": 0,
                "pending": 1,
            },
        )
        self.assertEqual(accounting["ingress_drop_events"], 1)
        self.assertEqual(accounting["phy_drop_events"], 2)

    def test_delivery_wins_over_drop_and_keeps_retry_observations(self) -> None:
        outcomes = terminal_packet_outcomes(
            [attempt("p1", "h1")],
            [{"packet_id": "p1"}, {"packet_id": "p1"}],
            [
                {"event": "backoff", "transport_payload_sha256": "h1"},
                {"event": "retry", "transport_payload_sha256": "h1"},
                {
                    "event": "drop",
                    "drop_reason": "retry_limit_payload",
                    "transport_payload_sha256": "h1",
                },
            ],
        )

        outcome = outcomes["p1"]
        self.assertEqual(outcome["status"], "delivered")
        self.assertIsNone(outcome["drop_reason"])
        self.assertEqual(outcome["delivery_count"], 2)
        self.assertEqual(outcome["duplicate_deliveries"], 1)
        self.assertEqual(outcome["backoff_events"], 1)
        self.assertEqual(outcome["retry_events"], 2)

    def test_ingress_deadline_and_queue_deadline_are_distinct(self) -> None:
        outcomes = terminal_packet_outcomes(
            [attempt("ingress", "h1"), attempt("queue", "h2")],
            [],
            [
                {
                    "event": "admission_drop",
                    "drop_reason": "ingress_deadline_payload",
                    "packet_id": "ingress",
                },
                {
                    "event": "drop",
                    "drop_reason": "deadline_drop_payload",
                    "transport_payload_sha256": "h2",
                },
            ],
        )

        self.assertEqual(outcomes["ingress"]["status"], "dropped_at_ingress")
        self.assertEqual(outcomes["queue"]["status"], "dropped_in_medium")

    def test_retry_exhaustion_is_one_medium_terminal_outcome(self) -> None:
        events = [
            {"event": "backoff", "transport_payload_sha256": "h1"},
            {"event": "backoff", "transport_payload_sha256": "h1"},
            {"event": "retry", "transport_payload_sha256": "h1"},
            {
                "event": "drop",
                "drop_reason": "retry_limit_additional_data",
                "transport_payload_sha256": "h1",
            },
        ]
        outcome = terminal_packet_outcomes([attempt("p1", "h1")], [], events)["p1"]

        self.assertEqual(outcome["status"], "dropped_in_medium")
        self.assertEqual(outcome["drop_reason"], "retry_limit_additional_data")
        self.assertEqual(outcome["backoff_events"], 2)
        self.assertEqual(outcome["retry_events"], 3)
        self.assertEqual(outcome["drop_event_count"], 1)

    def test_drain_finalization_eliminates_pending_and_preserves_invariant(self) -> None:
        attempts = [attempt("p1", "h1")]
        pending = account_packets(attempts, [], [])
        finalized = account_packets(attempts, [], [], finalize_pending=True)
        outcome = terminal_packet_outcomes(
            attempts, [], [], finalize_pending=True
        )["p1"]

        self.assertEqual(pending["packets_pending"], 1)
        self.assertFalse(pending["all_packets_terminal"])
        self.assertEqual(finalized["packets_pending"], 0)
        self.assertEqual(finalized["packets_dropped_unique"], 1)
        self.assertEqual(finalized["packets_expired_at_drain"], 1)
        self.assertEqual(finalized["terminal_status_counts"]["expired_at_drain"], 1)
        self.assertTrue(finalized["all_packets_terminal"])
        self.assertTrue(finalized["packet_invariant_holds"])
        self.assertEqual(outcome["status"], "expired_at_drain")
        self.assertEqual(outcome["drop_reason"], "drain_expired")


if __name__ == "__main__":
    unittest.main()
