"""Unit tests for logical packet/event accounting."""

from __future__ import annotations

import unittest

from network.scripts.packet_accounting import account_packets


def attempt(packet_id: str, *hashes: str) -> dict[str, object]:
    return {"packet_id": packet_id, "fragment_hashes": list(hashes)}


class PacketAccountingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
