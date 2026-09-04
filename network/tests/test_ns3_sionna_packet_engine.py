#!/usr/bin/env python3
"""Compiled M4 tests for applied Sionna state in the ns-3 packet path."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from network.ns3.tap_packet_engine_config import ConfigError, EngineConfig, from_repository
from network.radio_provider.sionna_packet_adapter import (
    AppliedStateIPCWriter,
    deterministic_loss_sample,
)


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "network/ns3/run_ns3_tap_packet_engine.sh"
BINARY = Path(
    os.environ.get(
        "AMS_NS3_PACKET_ENGINE_BINARY",
        ROOT / ".external/ns-3/build/scratch/ns3.40-ams-tap-packet-engine-default",
    )
)
NS3_LIB = Path(
    os.environ.get("AMS_NS3_PACKET_ENGINE_LIB_DIR", ROOT / ".external/ns-3/build/lib")
)
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
EXPECTED_CELLS = {
    (link, traffic_class)
    for uav in range(1, 6)
    for link in (f"cp>uav{uav}", f"uav{uav}>cp")
    for traffic_class in TRAFFIC_CLASSES
}


def binary_environment() -> dict[str, str]:
    environment = os.environ.copy()
    previous = environment.get("LD_LIBRARY_PATH", "")
    environment["LD_LIBRARY_PATH"] = (
        f"{NS3_LIB}:{previous}" if previous else str(NS3_LIB)
    )
    return environment


def enabled_config(state_file: Path, intervention: str) -> EngineConfig:
    return from_repository(
        uav_count=5,
        duration_ms=500,
        seed=42,
        run=1,
        event_epoch=17,
        self_test=True,
        self_test_burst=1,
        self_test_unknown_tos=False,
        sionna_ipc_enabled=True,
        sionna_state_file=str(state_file),
        sionna_poll_interval_ms=1,
        sionna_max_updates_per_poll=64,
        sionna_max_state_ttl_ms=1000,
        sionna_intervention=intervention,
    )


def write_fresh_states(
    state_file: Path,
    *,
    validity_ns: int = 900_000_000,
    loss_probability: float = 0.37,
    service_rate_overrides: dict[tuple[str, str], int] | None = None,
) -> dict[tuple[str, str], dict]:
    writer = AppliedStateIPCWriter(state_file)
    output: dict[tuple[str, str], dict] = {}
    applied_at = time.monotonic_ns()
    sequence = 0
    for link, traffic_class in sorted(EXPECTED_CELLS):
        sequence += 1
        query_id = f"query-{sequence}"
        applied_state_id = f"applied-{sequence}"
        service_rate_bps = (service_rate_overrides or {}).get(
            (link, traffic_class),
            2_000_000 if link.startswith("cp>") else 20_000_000,
        )
        record = writer.write(
            {
                "availability": "fresh",
                "unavailable_reason": None,
                "run_id": "m4-compiled-test",
                "profile": "m4_capacity",
                "phase_id": "phase-main",
                "directed_link": link,
                "traffic_class": traffic_class,
                "source_packet_event_epoch": 17,
                "source_packet_event_sequence": sequence,
                "source_packet_uid": sequence,
                "source_packet_causal_sha256": hashlib.sha256(
                    f"source-{sequence}".encode()
                ).hexdigest(),
                "query_id": query_id,
                "node_state_seq": 1,
                "query_wire_sha256": hashlib.sha256(
                    f"query-wire-{sequence}".encode()
                ).hexdigest(),
                "result_wire_sha256": hashlib.sha256(
                    f"result-wire-{sequence}".encode()
                ).hexdigest(),
                "applied_state_id": applied_state_id,
                "validity_start_monotonic_ns": applied_at,
                "expires_monotonic_ns": applied_at + validity_ns,
                "adapter_applied_monotonic_ns": applied_at,
                "physical": {
                    "sinr_db": 9.0,
                    "propagation_delay_ns": 1000 + sequence,
                },
                "effects": {
                    "mapping_version": "sinr-rate-per-v2",
                    "mapping_seed": 42,
                    "propagation_delay_ns": 1000 + sequence,
                    "loss_probability": loss_probability,
                    "service_rate_bps": service_rate_bps,
                    "reference_loss_sample": 0.5,
                    "reference_delivery": "deliver",
                    "intervention": "natural",
                },
            }
        )
        output[(link, traffic_class)] = dict(record)
    return output


def append_superseding_state(
    state_file: Path,
    original: dict,
    *,
    validity_ns: int,
) -> dict:
    lines = read_events(state_file)
    payload = {
        key: value
        for key, value in original.items()
        if key not in {"schema", "state_sequence", "state_sha256"}
    }
    now = time.monotonic_ns()
    payload.update(
        {
            "query_id": f"{original['query_id']}-superseding",
            "applied_state_id": f"{original['applied_state_id']}-superseding",
            "query_wire_sha256": hashlib.sha256(b"superseding-query").hexdigest(),
            "result_wire_sha256": hashlib.sha256(b"superseding-result").hexdigest(),
            "validity_start_monotonic_ns": now,
            "expires_monotonic_ns": now + validity_ns,
            "adapter_applied_monotonic_ns": now,
        }
    )
    record = {
        "schema": "ams.sionna.packet_state/v1",
        "state_sequence": max(int(item["state_sequence"]) for item in lines) + 1,
        **payload,
    }
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["state_sha256"] = hashlib.sha256(canonical).hexdigest()
    with state_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return record


def run_engine(config: EngineConfig, events: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BINARY), *config.engine_argv(events_file=str(events))],
        cwd=ROOT,
        env=binary_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class SionnaPacketEngineConfigTests(unittest.TestCase):
    def test_feature_flagged_runner_uses_the_real_config_cli_switch(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("--sionna-ipc\n", source)
        self.assertNotIn("--sionna-ipc-enabled", source)

    def test_m4_inputs_are_feature_flagged_and_hash_covered(self) -> None:
        baseline = from_repository(
            uav_count=5,
            duration_ms=500,
            seed=42,
            run=1,
            event_epoch=17,
            self_test=True,
            self_test_burst=1,
            self_test_unknown_tos=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            enabled = enabled_config(Path(temporary) / "states.jsonl", "natural")
            self.assertNotIn("sionna_ipc_enabled", baseline.canonical_text())
            self.assertIn("sionna_ipc_enabled=1", enabled.canonical_text())
            mutations = (
                dataclasses.replace(enabled, sionna_state_file="different.jsonl"),
                dataclasses.replace(enabled, sionna_poll_interval_ms=2),
                dataclasses.replace(enabled, sionna_max_updates_per_poll=63),
                dataclasses.replace(enabled, sionna_max_state_ttl_ms=999),
                dataclasses.replace(enabled, sionna_intervention="force_drop"),
            )
            self.assertTrue(all(item.sha256() != enabled.sha256() for item in mutations))
            argv = "\n".join(enabled.engine_argv(events_file="events.jsonl"))
            self.assertIn("--sionnaIpcEnabled=1", argv)
            self.assertIn("--sionnaStateFile=", argv)

    def test_invalid_m4_bounds_and_interventions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline = enabled_config(Path(temporary) / "states.jsonl", "natural")
            invalid = (
                dataclasses.replace(baseline, sionna_state_file=""),
                dataclasses.replace(baseline, sionna_poll_interval_ms=0),
                dataclasses.replace(baseline, sionna_max_updates_per_poll=0),
                dataclasses.replace(baseline, sionna_max_state_ttl_ms=60001),
                dataclasses.replace(baseline, sionna_intervention="hold_last"),
            )
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    with self.assertRaises(ConfigError):
                        candidate.validate()


@unittest.skipUnless(
    BINARY.is_file() and os.access(BINARY, os.X_OK), "compiled ns-3 engine absent"
)
class SionnaPacketEngineCompiledTests(unittest.TestCase):
    def test_unterminated_jsonl_tail_is_retried_without_ipc_fault(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / "states.jsonl"
            write_fresh_states(state_file)
            # The writer can be observed between a record write and its final
            # newline.  Valid complete records must remain usable while that
            # tail is retried, rather than faulting the entire state table.
            with state_file.open("ab") as stream:
                stream.write(b'{"incomplete":')
            events_file = root / "events.jsonl"
            result = run_engine(enabled_config(state_file, "force_deliver"), events_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = read_events(events_file)

        decisions = [
            event
            for event in events
            if event["event"] == "channel" and not event["p2mp"]
        ]
        self.assertEqual(len(decisions), 31)
        self.assertEqual(
            {(event["directed_link"], event["traffic_class"]) for event in decisions},
            EXPECTED_CELLS,
        )
        self.assertFalse(
            any(
                event["event"] == "drop"
                and event["drop_reason"] == "sionna_state_ipc_fault"
                for event in events
            )
        )

    def test_thirty_cells_apply_exact_state_lineage_delay_and_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / "states.jsonl"
            expected = write_fresh_states(state_file)
            events_file = root / "events.jsonl"
            result = run_engine(enabled_config(state_file, "force_deliver"), events_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = read_events(events_file)

        decisions = [
            event
            for event in events
            if event["event"] == "channel" and not event["p2mp"]
        ]
        self.assertEqual(
            {(event["directed_link"], event["traffic_class"]) for event in decisions},
            EXPECTED_CELLS,
        )
        self.assertEqual(len(decisions), 31)  # 30 cells plus the configured burst frame.
        for event in decisions:
            state = expected[(event["directed_link"], event["traffic_class"])]
            effects = state["effects"]
            self.assertEqual(event["radio_state_status"], "fresh")
            self.assertEqual(event["radio_state_sequence"], state["state_sequence"])
            self.assertEqual(event["radio_state_sha256"], state["state_sha256"])
            self.assertEqual(event["radio_query_id"], state["query_id"])
            self.assertEqual(event["radio_applied_state_id"], state["applied_state_id"])
            self.assertEqual(
                event["radio_result_wire_sha256"], state["result_wire_sha256"]
            )
            self.assertEqual(event["radio_delay_ns"], effects["propagation_delay_ns"])
            self.assertEqual(
                event["radio_loss_probability"], effects["loss_probability"]
            )
            self.assertEqual(
                event["radio_service_rate_bps"], effects["service_rate_bps"]
            )
            self.assertGreater(event["radio_serialization_time_ns"], 0)
            self.assertEqual(
                event["radio_rate_applied_at_monotonic_ns"],
                event["radio_delay_applied_at_monotonic_ns"],
            )
            self.assertEqual(
                event["radio_applied_device_id"],
                event["directed_link"].split(">", 1)[0] + ".radio",
            )
            self.assertEqual(event["radio_delivery"], "deliver")
            self.assertEqual(event["radio_intervention"], "force_deliver")
            self.assertEqual(
                event["radio_loss_sample"],
                deterministic_loss_sample(
                    event["transport_payload_sha256"], state["applied_state_id"], 42
                ),
            )
        self.assertEqual(
            {event["radio_service_rate_bps"] for event in decisions},
            {2_000_000, 20_000_000},
        )
        # Same-time different-link frames must retain their own state instead
        # of inheriting a worst/global link rate or propagation delay.
        for event in decisions:
            state = expected[(event["directed_link"], event["traffic_class"])]
            self.assertEqual(
                event["radio_serialization_time_ns"],
                (
                    event["packet_wire_size"] * 8 * 1_000_000_000
                    + state["effects"]["service_rate_bps"]
                    - 1
                )
                // state["effects"]["service_rate_bps"],
            )
            base_serialization = (
                event["packet_wire_size"] * 8 * 1_000_000_000 + 20_000_000 - 1
            ) // 20_000_000
            self.assertEqual(
                event["radio_base_serialization_time_ns"], base_serialization
            )
            self.assertEqual(
                event["radio_service_padding_ns"],
                max(0, event["radio_serialization_time_ns"] - base_serialization),
            )
            self.assertEqual(
                event["radio_effective_channel_delay_ns"],
                state["effects"]["propagation_delay_ns"]
                + event["radio_service_padding_ns"],
            )

    def test_force_drop_changes_delivery_and_preserves_physical_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / "states.jsonl"
            write_fresh_states(state_file)
            deliver_file = root / "deliver.jsonl"
            drop_file = root / "drop.jsonl"
            deliver = run_engine(
                enabled_config(state_file, "force_deliver"), deliver_file
            )
            drop = run_engine(enabled_config(state_file, "force_drop"), drop_file)
            self.assertEqual(deliver.returncode, 0, deliver.stderr)
            self.assertEqual(drop.returncode, 0, drop.stderr)
            deliver_events = read_events(deliver_file)
            drop_events = read_events(drop_file)

        delivered = {
            (event["directed_link"], event["traffic_class"], event["transport_payload_sha256"]): event
            for event in deliver_events
            if event["event"] == "dequeue" and not event["p2mp"]
        }
        dropped = {
            (event["directed_link"], event["traffic_class"], event["transport_payload_sha256"]): event
            for event in drop_events
            if event["event"] == "drop" and event["drop_reason"] == "sionna_loss"
        }
        self.assertEqual(set(delivered), set(dropped))
        for key in delivered:
            left, right = delivered[key], dropped[key]
            for field in (
                "radio_state_sequence",
                "radio_state_sha256",
                "radio_query_id",
                "radio_applied_state_id",
                "radio_result_wire_sha256",
                "radio_mapping_version",
                "radio_mapping_seed",
                "radio_delay_ns",
                "radio_loss_probability",
                "radio_loss_sample",
                "radio_service_rate_bps",
            ):
                self.assertEqual(left[field], right[field], field)
            self.assertEqual(left["radio_delivery"], "deliver")
            self.assertEqual(right["radio_delivery"], "drop")
        self.assertFalse(
            any(
                event["event"] == "channel" and not event["p2mp"]
                for event in drop_events
            )
        )

    def test_corrupt_missing_and_expired_state_fail_closed(self) -> None:
        cases: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrupt = root / "corrupt.jsonl"
            write_fresh_states(corrupt)
            raw = corrupt.read_text(encoding="utf-8")
            corrupt.write_text(raw.replace('"mapping_seed":42', '"mapping_seed":43', 1))
            cases.append(("ipc_fault", str(corrupt)))

            missing = root / "missing.jsonl"
            missing.write_text("", encoding="utf-8")
            cases.append(("missing", str(missing)))

            expired = root / "expired.jsonl"
            write_fresh_states(expired, validity_ns=1)
            cases.append(("expired", str(expired)))

            for expected_status, state_path in cases:
                with self.subTest(status=expected_status):
                    events_file = root / f"{expected_status}.events.jsonl"
                    result = run_engine(
                        enabled_config(Path(state_path), "force_deliver"), events_file
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    events = read_events(events_file)
                    failures = [
                        event
                        for event in events
                        if event["event"] == "drop"
                        and event["drop_reason"]
                        == f"sionna_state_{expected_status}"
                    ]
                    self.assertGreaterEqual(len(failures), 31)
                    self.assertFalse(
                        any(
                            event["event"] == "channel" and not event["p2mp"]
                            for event in events
                        )
                    )

    def test_packet_waiting_in_queue_is_dropped_when_its_state_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / "expiry-race.states.jsonl"
            expected = write_fresh_states(
                state_file,
                validity_ns=250_000_000,
                service_rate_overrides={("cp>uav1", "payload"): 1_000},
            )
            events_file = root / "expiry-race.events.jsonl"
            config = dataclasses.replace(
                enabled_config(state_file, "force_deliver"),
                duration_ms=900,
                self_test_burst=8,
            )
            result = run_engine(config, events_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = read_events(events_file)

        drops = [
            event
            for event in events
            if event["event"] == "drop"
            and event["drop_reason"] == "sionna_state_expired_in_queue"
        ]
        self.assertGreater(len(drops), 0)
        channel_hashes = {
            event["packet_wire_hash"]
            for event in events
            if event["event"] == "channel"
        }
        for event in drops:
            state = expected[(event["directed_link"], event["traffic_class"])]
            self.assertEqual(event["radio_state_status"], "expired_in_queue")
            self.assertEqual(event["radio_state_sequence"], state["state_sequence"])
            self.assertEqual(event["radio_state_sha256"], state["state_sha256"])
            self.assertNotIn(event["packet_wire_hash"], channel_hashes)

    def test_packet_waiting_in_queue_is_dropped_when_state_is_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / "supersede-race.states.jsonl"
            expected = write_fresh_states(
                state_file,
                validity_ns=2_000_000_000,
                service_rate_overrides={("cp>uav1", "payload"): 1_000},
            )
            target_cell = ("cp>uav2", "control")
            newer = append_superseding_state(
                state_file,
                expected[target_cell],
                validity_ns=2_000_000_000,
            )
            events_file = root / "supersede-race.events.jsonl"
            config = dataclasses.replace(
                enabled_config(state_file, "force_deliver"),
                duration_ms=1_300,
                self_test_burst=1,
                sionna_poll_interval_ms=100,
                sionna_max_updates_per_poll=30,
                sionna_max_state_ttl_ms=3_000,
                # Isolate state supersession from the newer 250 ms product
                # control deadline.  The deliberately 1 kbit/s leading frame
                # must remain queued long enough for the second IPC poll.
                queue_control_deadline_ms=1_200,
                queue_control_max_age_ms=1_200,
            )
            result = run_engine(config, events_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = read_events(events_file)

        drops = [
            event
            for event in events
            if event["event"] == "drop"
            and event["drop_reason"] == "sionna_state_superseded_in_queue"
        ]
        self.assertGreater(len(drops), 0)
        self.assertTrue(
            all(
                (event["directed_link"], event["traffic_class"]) == target_cell
                for event in drops
            )
        )
        channel_hashes = {
            event["packet_wire_hash"]
            for event in events
            if event["event"] == "channel"
        }
        for event in drops:
            self.assertEqual(event["radio_state_status"], "superseded_in_queue")
            self.assertEqual(
                event["radio_state_sequence"], expected[target_cell]["state_sequence"]
            )
            self.assertNotEqual(event["radio_state_sequence"], newer["state_sequence"])
            self.assertNotIn(event["packet_wire_hash"], channel_hashes)

    def test_busy_channel_keeps_other_owner_queued_until_uplink_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / "device-held-race.states.jsonl"
            expected = write_fresh_states(
                state_file,
                validity_ns=2_000_000_000,
                service_rate_overrides={("cp>uav1", "payload"): 1_000},
            )
            target_cell = ("uav2>cp", "control")
            newer = append_superseding_state(
                state_file,
                expected[target_cell],
                validity_ns=2_000_000_000,
            )
            events_file = root / "device-held-race.events.jsonl"
            config = dataclasses.replace(
                enabled_config(state_file, "force_deliver"),
                duration_ms=1_300,
                self_test_burst=1,
                sionna_poll_interval_ms=100,
                sionna_max_updates_per_poll=30,
                sionna_max_state_ttl_ms=3_000,
                # Exercise uplink state revalidation behind a busy shared
                # channel, not the independent product deadline-drop mechanism.
                queue_control_deadline_ms=1_200,
                queue_control_max_age_ms=1_200,
            )
            result = run_engine(config, events_file)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = read_events(events_file)

        source_state = expected[target_cell]
        queue_drops = [
            event
            for event in events
            if event["event"] == "drop"
            and (event["directed_link"], event["traffic_class"]) == target_cell
            and event["drop_reason"] == "sionna_state_superseded_in_queue"
        ]
        self.assertEqual(len(queue_drops), 1)
        dropped = queue_drops[0]
        dropped_hash = dropped["packet_wire_hash"]
        self.assertEqual(dropped["radio_state_status"], "superseded_in_queue")
        self.assertEqual(dropped["radio_state_sequence"], source_state["state_sequence"])
        self.assertNotEqual(dropped["radio_state_sequence"], newer["state_sequence"])
        self.assertEqual(dropped["device_id"], "uav2.radio")
        self.assertFalse(
            any(
                event["event"] in {"channel", "egress", "backoff"}
                and event["packet_wire_hash"] == dropped_hash
                for event in events
            )
        )


if __name__ == "__main__":
    unittest.main()
