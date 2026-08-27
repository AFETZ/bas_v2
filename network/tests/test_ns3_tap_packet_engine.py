"""Static, adversarial, and compiled tests for the external TapBridge engine."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from network.ns3.tap_packet_engine_config import (
    ConfigError,
    EngineConfig,
    from_repository,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "network/ns3/scratch/ams-tap-packet-engine.cc"
BUILD_SCRIPT = ROOT / "network/ns3/build_ns3_tap_packet_engine.sh"
CORE_PATCH = (
    ROOT / "network/ns3/patches/ns-3.40-csma-global-queue-scheduler.patch"
)
OLD_DIAGNOSTIC = ROOT / "network/ns3/scratch/ams-tap-vertical-slice.cc"
BINARY = Path(
    os.environ.get(
        "AMS_NS3_PACKET_ENGINE_BINARY",
        ROOT / ".external/ns-3/build/scratch/ns3.40-ams-tap-packet-engine-default",
    )
)
NS3_LIB = Path(
    os.environ.get("AMS_NS3_PACKET_ENGINE_LIB_DIR", ROOT / ".external/ns-3/build/lib")
)


def config_for(**overrides: object) -> EngineConfig:
    values: dict[str, object] = {
        "uav_count": 5,
        "duration_ms": 500,
        "seed": 42,
        "run": 1,
        "event_epoch": 7,
        "self_test": True,
        "self_test_burst": 1,
        "self_test_unknown_tos": False,
    }
    values.update(overrides)
    return from_repository(**values)  # type: ignore[arg-type]


def binary_environment() -> dict[str, str]:
    environment = os.environ.copy()
    previous = environment.get("LD_LIBRARY_PATH", "")
    environment["LD_LIBRARY_PATH"] = (
        f"{NS3_LIB}:{previous}" if previous else str(NS3_LIB)
    )
    return environment


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


class TapPacketEngineConfigTests(unittest.TestCase):
    def test_repository_resolution_supports_exact_one_and_five_uav_profiles(
        self,
    ) -> None:
        one = config_for(uav_count=1)
        five = config_for(uav_count=5)
        self.assertEqual(one.tap_uavs, ("tap-uav",))
        self.assertEqual(
            five.tap_uavs,
            ("tap-uav1", "tap-uav2", "tap-uav3", "tap-uav4", "tap-uav5"),
        )
        self.assertEqual(five.radio_rate, "20000000bps")
        self.assertEqual(five.radio_delay, "2ms")
        self.assertEqual(
            (
                five.queue_control_max_packets,
                five.queue_payload_max_packets,
                five.queue_additional_data_max_packets,
            ),
            (256, 128, 128),
        )
        self.assertEqual(one.sha256(), one.sha256())
        self.assertRegex(five.sha256(), r"^[0-9a-f]{64}$")
        self.assertNotEqual(one.sha256(), five.sha256())

    def test_every_packet_behavior_input_changes_config_hash(self) -> None:
        baseline = config_for()
        mutations = (
            dataclasses.replace(baseline, uav_count=4, tap_uavs=baseline.tap_uavs[:4]),
            dataclasses.replace(baseline, duration_ms=501),
            dataclasses.replace(baseline, radio_rate="25000000bps"),
            dataclasses.replace(baseline, radio_delay="3ms"),
            dataclasses.replace(baseline, queue_control_max_packets=255),
            dataclasses.replace(baseline, queue_payload_max_packets=127),
            dataclasses.replace(baseline, queue_additional_data_max_packets=127),
            dataclasses.replace(baseline, queue_control_deadline_ms=251),
            dataclasses.replace(baseline, queue_payload_max_age_ms=751),
            dataclasses.replace(baseline, shaping_enabled=False),
            dataclasses.replace(baseline, control_reserved_bps=3_999_999),
            dataclasses.replace(baseline, payload_admission_rate_bps=6_499_999),
            dataclasses.replace(baseline, token_bucket_burst_bytes_per_uav=6_999),
            dataclasses.replace(baseline, lower_retry_limit=15),
            dataclasses.replace(baseline, mac_retry_limit=17),
            dataclasses.replace(baseline, event_log_flush_every=255),
            dataclasses.replace(baseline, event_log_flush_max_delay_ms=24),
            dataclasses.replace(baseline, control_priority=3, payload_priority=4, additional_data_priority=5),
            dataclasses.replace(baseline, control_tos=185),
            dataclasses.replace(baseline, seed=43),
            dataclasses.replace(baseline, run=2),
            dataclasses.replace(baseline, event_epoch=8),
            dataclasses.replace(baseline, self_test_burst=2),
            dataclasses.replace(baseline, self_test_unknown_tos=True),
        )
        self.assertEqual(len({item.sha256() for item in mutations}), len(mutations))
        self.assertNotIn(baseline.sha256(), {item.sha256() for item in mutations})

    def test_adversarial_configs_fail_closed(self) -> None:
        baseline = config_for()
        invalid = (
            dataclasses.replace(baseline, uav_count=0, tap_uavs=()),
            dataclasses.replace(
                baseline, uav_count=6, tap_uavs=baseline.tap_uavs + ("tap-uav6",)
            ),
            dataclasses.replace(baseline, tap_uavs=("tap-uav1",) * 5),
            dataclasses.replace(baseline, tap_gcs="tap-uav1"),
            dataclasses.replace(baseline, tap_gcs="interface-name-is-too-long"),
            dataclasses.replace(baseline, duration_ms=0),
            dataclasses.replace(baseline, radio_rate="20.5Mbps"),
            dataclasses.replace(baseline, radio_delay="0ms"),
            dataclasses.replace(baseline, queue_control_max_packets=0),
            dataclasses.replace(baseline, queue_payload_max_packets=1_000_001),
            dataclasses.replace(baseline, queue_control_deadline_ms=0),
            dataclasses.replace(baseline, queue_control_max_age_ms=251),
            dataclasses.replace(baseline, strict_control_priority=False),
            dataclasses.replace(baseline, fair_lower_classes_per_uav=False),
            dataclasses.replace(baseline, ingress_protection_enabled=False),
            dataclasses.replace(baseline, shaping_enabled=1),
            dataclasses.replace(baseline, control_reserved_bps=0),
            dataclasses.replace(baseline, payload_admission_rate_bps=16_000_001),
            dataclasses.replace(baseline, token_bucket_burst_bytes_per_uav=0),
            dataclasses.replace(baseline, lower_retry_limit=65),
            dataclasses.replace(baseline, event_log_flush_every=0),
            dataclasses.replace(baseline, event_log_flush_max_delay_ms=0),
            dataclasses.replace(baseline, payload_priority=0),
            dataclasses.replace(baseline, payload_tos=184),
            dataclasses.replace(baseline, seed=0),
            dataclasses.replace(baseline, run=0),
            dataclasses.replace(baseline, event_epoch=0),
            dataclasses.replace(baseline, self_test_burst=0),
            dataclasses.replace(baseline, self_test=False, self_test_unknown_tos=True),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ConfigError):
                    candidate.validate()

    def test_engine_argv_carries_hash_seed_epoch_and_explicit_bounds(self) -> None:
        config = config_for()
        argv = config.engine_argv(events_file="events.jsonl")
        joined = "\n".join(argv)
        for expected in (
            f"--configHash={config.sha256()}",
            "--seed=42",
            "--run=1",
            "--eventEpoch=7",
            "--uavCount=5",
            "--queueControlMaxPackets=256",
            "--queuePayloadMaxPackets=128",
            "--queueAdditionalDataMaxPackets=128",
            "--queueControlDeadlineMs=250",
            "--queuePayloadMaxAgeMs=750",
            "--strictControlPriority=1",
            "--fairLowerClassesPerUav=1",
            "--ingressProtectionEnabled=1",
            "--shapingEnabled=1",
            "--controlReservedBps=4000000",
            "--payloadAdmissionRateBps=6500000",
            "--additionalDataAdmissionRateBps=6500000",
            "--tokenBucketBurstBytesPerUav=7000",
            "--lowerRetryLimit=16",
            "--macRetryLimit=64",
            "--eventLogFlushEvery=256",
            "--eventLogFlushMaxDelayMs=25",
            "--controlTos=184",
        ):
            self.assertIn(expected, joined)

    def test_meltdown_shaping_requires_a_separate_startup_mode(self) -> None:
        protected = config_for()
        meltdown = config_for(engine_profile="meltdown")
        self.assertTrue(protected.shaping_enabled)
        self.assertFalse(meltdown.shaping_enabled)
        self.assertNotEqual(protected.sha256(), meltdown.sha256())
        self.assertIn(
            "--shapingEnabled=0",
            meltdown.engine_argv(events_file="events.jsonl"),
        )

        source = SOURCE.read_text(encoding="utf-8")
        controller = source[
            source.index("class IngressProtectionController") : source.index(
                "class PerUavRoundRobin"
            )
        ]
        self.assertNotIn("metadata.applicationProfileId ==", controller)
        self.assertNotIn("m_shapingBypass", controller)

    def test_overload_profile_must_exceed_the_configured_radio_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            radio_path = Path(temporary) / "radio.yaml"
            source = (ROOT / "network/config/radio_24ghz.yaml").read_text(
                encoding="utf-8"
            )
            radio_path.write_text(
                source.replace(
                    "channel_rate_bps: 20000000",
                    "channel_rate_bps: 40000000",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError, "offered load must exceed.*radio capacity"
            ):
                config_for(radio_path=radio_path)


class TapPacketEngineStaticTests(unittest.TestCase):
    def test_source_uses_strict_control_and_fair_lower_scheduler(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        queue_start = source.index("class StrictPriorityScheduler")
        queue_end = source.index("struct EngineConfig", queue_start)
        queue_source = source[queue_start:queue_end]

        self.assertIn("counts[CONTROL_CLASS] > 0", queue_source)
        self.assertIn("counts[m_nextLowerClass] > 0", queue_source)
        self.assertIn(
            "m_nextLowerClass = selectedClass == PAYLOAD_CLASS",
            queue_source,
        )
        self.assertIn("const auto selected = SelectNextIterator();", queue_source)
        self.assertNotRegex(
            queue_source,
            r"Do(?:Dequeue|Remove|Peek)\(GetContainer\(\)\.begin\(\)\)",
        )
        self.assertGreaterEqual(
            queue_source.count("m_scheduler.Record(classIndex"),
            2,
        )
        self.assertIn(
            "StrictPriorityScheduler::DeterministicSelfTest()",
            source,
        )
        self.assertIn("IngressProtection", queue_source)
        self.assertIn("ingress_token_bucket_", source)
        self.assertIn("deadline_drop_", queue_source)

    def test_global_scheduler_is_owner_exact_and_core_patch_is_reproducible(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        patch = CORE_PATCH.read_text(encoding="utf-8")
        build = BUILD_SCRIPT.read_text(encoding="utf-8")

        scheduler = source[
            source.index("class GlobalRadioScheduler") : source.index(
                "struct EngineConfig"
            )
        ]
        self.assertIn("Snapshot()", scheduler)
        self.assertIn("counts[CONTROL_CLASS]", source)
        self.assertIn("selected->ownerIndex", scheduler)
        self.assertIn("GrantExactPacket(selected->entryId", scheduler)
        self.assertIn("owner.device->StartOneQueuedPacket()", scheduler)
        self.assertIn("m_perUavSchedulers", scheduler)
        self.assertIn("expectedUav <= MAX_UAVS", scheduler)
        self.assertIn("ownerIndex != expectedUav", scheduler)
        self.assertIn("selected->packetUid != 20", scheduler)
        self.assertIn("selected->ownerIndex != 5", scheduler)
        self.assertIn("A stale grant is fail-closed", source)

        for hook in (
            "SetQueueReadyCallback",
            "IsTransmitReady",
            "StartOneQueuedPacket",
            "RequestCurrentPacketAbortOnNextBusy",
            "SetIdleCallback",
        ):
            self.assertIn(hook, patch)
        self.assertIn("m_idleCallback()", patch)
        self.assertIn("m_queueReadyCallback.IsNull()", patch)
        self.assertIn("patch --batch --forward --dry-run", build)
        self.assertIn("patch --batch --reverse --dry-run", build)
        self.assertIn("--expected-core-tree-sha256", build)

    def test_pre_admission_suppression_and_retry_abort_are_fail_closed(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        trace_ingress = source[
            source.index("TraceIngress(") : source.index("TraceIpv4Drop(")
        ]
        self.assertIn("metadata.sourceMonotonicValid", trace_ingress)
        self.assertNotIn("applicationProfileId > 0", trace_ingress)

        trace_backoff = source[
            source.index("TraceBackoff(") : source.index("TracePhyTxEnd(")
        ]
        self.assertIn("RequestCurrentPacketAbortOnNextBusy", trace_backoff)
        self.assertNotIn("SetBackoffParams", trace_backoff)

    def test_event_logger_has_config_driven_bounded_timed_flush(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        logger = source[
            source.index("class PacketEventLogger") : source.index("TraceIngress(")
        ]
        self.assertIn("ScheduleTimedFlush", logger)
        self.assertIn("TimedFlush", logger)
        self.assertIn("MilliSeconds(m_flushMaxDelayMs)", logger)
        self.assertIn("m_eventsSinceFlush > 0", logger)
        self.assertNotIn("MilliSeconds(25)", logger)
        self.assertIn(
            'command.AddValue("eventLogFlushMaxDelayMs"',
            source,
        )
        self.assertIn(
            '"event_log_flush_max_delay_ms=" << config.eventLogFlushMaxDelayMs',
            source,
        )

    def test_source_uses_external_tap_netdevices_and_no_ns3_applications(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("TapBridgeHelper", source)
        self.assertIn('StringValue("UseBridge")', source)
        self.assertIn("CsmaHelper radio", source)
        self.assertIn("AmsThreeClassQueue", source)
        self.assertIn('TraceConnect("MacPromiscRx"', source)
        self.assertNotIn("applications-module", source)
        self.assertIsNone(
            re.search(r"\b(?:OnOff|PacketSink|Application)Helper\b", source)
        )
        self.assertNotIn("ApplicationContainer", source)
        self.assertIn("InternetStackHelper", source)
        self.assertIn("radio.Install(routers)", source)
        self.assertEqual(source.count("radio.Install(routers)"), 1)

    def test_source_traces_real_ipv4_no_route_and_reconstructs_udp_identity(
        self,
    ) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("ReconstructIpv4EthernetFrame", source)
        self.assertIn("reconstructedHeader.SetPayloadSize(frame->GetSize())", source)
        self.assertIn("frame->AddHeader(reconstructedHeader)", source)
        self.assertIn("reason != Ipv4L3Protocol::DROP_NO_ROUTE", source)
        self.assertIn('logger->Log("drop", context, frame, -1, -1, "ipv4_no_route")', source)
        self.assertRegex(
            source,
            r'routerIpv4->TraceConnect\(\s*"Drop"[\s\S]+?TraceIpv4Drop',
        )

    def test_source_declares_all_required_packet_stages_and_fields(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        for stage in ("ingress", "enqueue", "dequeue", "channel", "drop", "egress"):
            self.assertIn(f'"{stage}"', source)
        for field in (
            "event_epoch",
            "packet_wire_hash",
            "packet_wire_size",
            "packet_uid",
            "transport_payload_sha256",
            "transport_payload_size",
            "source_udp_port",
            "destination_udp_port",
            "dscp",
            "traffic_class",
            "directed_link",
            "queue_id",
            "device_id",
            "config_sha256",
            "root_transmission",
            "queue_age_ns",
            "scheduler_lag_ns",
        ):
            self.assertIn(field, source)

    def test_build_is_exact_ns340_and_uses_canonical_locked_module_union(self) -> None:
        build = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('NS3_VERSION" != "3.40"', build)
        modules = re.search(r'REQUIRED_MODULES="([^"]+)"', build)
        self.assertIsNotNone(modules)
        self.assertEqual(
            modules.group(1).split(","),
            [
                "applications",
                "bridge",
                "core",
                "csma",
                "flow-monitor",
                "internet",
                "mobility",
                "network",
                "stats",
                "tap-bridge",
                "traffic-control",
            ],
        )
        self.assertIn("ns3_build_receipt.py", build)
        self.assertIn("--program ams-tap-packet-engine", build)
        self.assertIn("--program ams-tap-vertical-slice", build)
        self.assertIn("./ns3 build scratch/ams-tap-vertical-slice", build)
        self.assertLess(
            build.index("./ns3 build scratch/ams-tap-packet-engine"),
            build.index("--program ams-tap-vertical-slice"),
        )
        self.assertTrue(
            OLD_DIAGNOSTIC.is_file(), "existing one-UAV diagnostic was removed"
        )


@unittest.skipUnless(
    BINARY.is_file() and os.access(BINARY, os.X_OK), "compiled ns-3 engine absent"
)
class TapPacketEngineCompiledTests(unittest.TestCase):
    def test_print_help_smoke(self) -> None:
        result = subprocess.run(
            [str(BINARY), "--PrintHelp"],
            cwd=ROOT,
            env=binary_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uavCount", result.stdout)
        self.assertIn("configHash", result.stdout)

    def test_cpp_and_python_canonical_hashes_are_identical(self) -> None:
        config = config_for()
        args = config.engine_argv(events_file="unused.jsonl")
        args = [
            argument for argument in args if not argument.startswith("--configHash=")
        ]
        result = subprocess.run(
            [str(BINARY), *args, "--printConfigHash=1"],
            cwd=ROOT,
            env=binary_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), config.sha256())

    def test_invalid_topology_and_queue_cli_values_fail_before_simulation(self) -> None:
        cases = (
            ("--uavCount=0",),
            ("--uavCount=6",),
            ("--uavCount=2", "--tapUavs=tap-uav1,tap-uav1"),
            ("--queueControlMaxPackets=0",),
            ("--eventEpoch=0",),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(BINARY), *arguments, "--printConfigHash=1"],
                    cwd=ROOT,
                    env=binary_environment(),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("FAIL", result.stderr)

    def test_short_five_uav_run_proves_single_root_p2mp_transmission(self) -> None:
        config = config_for()
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            result = run_engine(config, events)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["p2mp_root_transmissions"], 1)
            self.assertEqual(summary["p2mp_egress_devices"], 5)
            for self_test in (
                "token_bucket_self_test",
                "deadline_drop_self_test",
                "reserved_control_self_test",
                "profile_id_no_bypass_self_test",
                "per_uav_fairness_self_test",
                "retry_bound_self_test",
                "strict_control_priority_self_test",
                "global_radio_scheduler_self_test",
                "stale_grant_self_test",
            ):
                self.assertTrue(summary[self_test], self_test)
            records = [json.loads(line) for line in events.read_text().splitlines()]

        required = {
            "schema",
            "event_epoch",
            "event_sequence",
            "sim_time_ns",
            "event",
            "packet_wire_hash_algorithm",
            "packet_wire_hash",
            "packet_wire_size",
            "transport_payload_sha256",
            "transport_payload_size",
            "source_udp_port",
            "destination_udp_port",
            "dscp",
            "traffic_class",
            "directed_link",
            "queue_id",
            "device_id",
            "config_sha256",
        }
        self.assertTrue(records)
        self.assertTrue(all(required <= record.keys() for record in records))
        self.assertEqual({record["event_epoch"] for record in records}, {7})
        self.assertEqual(
            {record["config_sha256"] for record in records}, {config.sha256()}
        )
        self.assertTrue(
            {"ingress", "enqueue", "dequeue", "channel", "egress"}
            <= {record["event"] for record in records}
        )
        self.assertNotIn("unknown", {record["traffic_class"] for record in records})

        radio_cells = {
            (record["directed_link"], record["traffic_class"])
            for record in records
            if record["event"] == "channel" and not record["p2mp"]
        }
        expected_cells = {
            (link, traffic_class)
            for uav in range(1, 6)
            for link in (f"cp>uav{uav}", f"uav{uav}>cp")
            for traffic_class in ("control", "payload", "additional_data")
        }
        self.assertEqual(radio_cells, expected_cells)
        for record in records:
            if record["event"] != "channel" or record["p2mp"]:
                continue
            priority = {"control": 0, "payload": 1, "additional_data": 2}[
                record["traffic_class"]
            ]
            self.assertEqual(
                record["queue_id"],
                f"{record['directed_link']}.{record['traffic_class']}.q{priority}",
            )

        root = [
            record
            for record in records
            if record["event"] == "channel" and record["root_transmission"]
        ]
        egress = [
            record
            for record in records
            if record["event"] == "egress" and record["p2mp"]
        ]
        self.assertEqual(len(root), 1)
        self.assertEqual(len(egress), 5)
        self.assertEqual(
            {record["packet_uid"] for record in [*root, *egress]},
            {root[0]["packet_uid"]},
        )
        # Each routed external segment legitimately has a different Ethernet
        # wire image, while the ns-3 packet lineage remains the same.
        self.assertEqual(
            len({record["packet_wire_hash"] for record in [*root, *egress]}), 6
        )
        self.assertTrue(
            all(
                re.fullmatch(r"[0-9a-f]{64}", record["packet_wire_hash"])
                for record in [*root, *egress]
            )
        )
        self.assertEqual(
            {record["packet_wire_size"] for record in [*root, *egress]}, {64}
        )
        self.assertEqual(
            {record["directed_link"] for record in [*root, *egress]}, {"cp>p2mp"}
        )
        self.assertEqual(len({record["device_id"] for record in egress}), 5)
        self.assertEqual(
            {record["device_id"] for record in egress},
            {f"uav{index}.tap.egress" for index in range(1, 6)},
        )
        root_queue = [
            record
            for record in records
            if record["packet_uid"] == root[0]["packet_uid"]
            and record["event"] in {"enqueue", "dequeue", "channel"}
        ]
        self.assertEqual(
            {record["packet_wire_hash"] for record in root_queue},
            {root[0]["packet_wire_hash"]},
        )
        self.assertEqual(
            {record["transport_payload_sha256"] for record in [*root, *egress]},
            {root[0]["transport_payload_sha256"]},
        )
        self.assertEqual(
            {record["transport_payload_size"] for record in [*root, *egress]},
            {len("ams-self-test:31")},
        )
        legacy_drops = [
            record
            for record in records
            if record["event"] == "drop"
            and record["drop_reason"]
            == "udp_destination_port_not_in_endpoint_matrix"
        ]
        self.assertEqual(len(legacy_drops), 1)
        self.assertEqual(legacy_drops[0]["destination_udp_port"], 14550)
        legacy_payload_hash = legacy_drops[0]["transport_payload_sha256"]
        self.assertRegex(legacy_payload_hash, r"^[0-9a-f]{64}$")
        self.assertFalse(
            any(
                record["transport_payload_sha256"] == legacy_payload_hash
                and record["event"] in {"channel", "egress"}
                for record in records
            )
        )

        unreachable = [
            record
            for record in records
            if record["destination_ip"] == "198.18.0.1"
        ]
        self.assertEqual(
            [record["event"] for record in unreachable],
            ["ingress", "drop"],
        )
        self.assertEqual(unreachable[1]["drop_reason"], "ipv4_no_route")
        self.assertIsNone(unreachable[0]["drop_reason"])
        self.assertEqual(
            {
                (
                    record["transport_protocol"],
                    record["source_ip"],
                    record["destination_ip"],
                    record["source_udp_port"],
                    record["destination_udp_port"],
                    record["tos"],
                    record["transport_payload_sha256"],
                    record["transport_payload_size"],
                )
                for record in unreachable
            },
            {
                (
                    17,
                    "10.71.0.10",
                    "198.18.0.1",
                    20033,
                    15300,
                    0,
                    unreachable[0]["transport_payload_sha256"],
                    len("ams-self-test:33"),
                )
            },
        )
        self.assertRegex(
            unreachable[0]["transport_payload_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual(
            [
                record
                for record in records
                if "unknown" in record["directed_link"]
            ],
            unreachable,
        )
        allowed_ports = {
            *range(14600, 14606),
            *range(14700, 14706),
            *range(14800, 14806),
            14900,
        }
        self.assertTrue(
            all(
                record["destination_udp_port"] in allowed_ports
                for record in records
                if record["event"] in {"channel", "egress"}
                and record["transport_protocol"] == 17
            )
        )

    def test_same_seed_and_config_produce_byte_identical_event_streams(self) -> None:
        config = config_for(uav_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.jsonl"
            second = Path(temporary) / "second.jsonl"
            first_result = run_engine(config, first)
            second_result = run_engine(config, second)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_queue_overflow_unknown_dscp_and_hash_substitution_fail_closed(
        self,
    ) -> None:
        config = dataclasses.replace(
            config_for(uav_count=1),
            queue_payload_max_packets=1,
            self_test_burst=8,
            self_test_unknown_tos=True,
        )
        config.validate()
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "adversarial.jsonl"
            result = run_engine(config, events)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in events.read_text().splitlines()]
            reasons = {
                record["drop_reason"] for record in records if record["event"] == "drop"
            }
            self.assertIn("queue_limit_payload", reasons)
            self.assertIn("unmapped_dscp_or_ether_type", reasons)

            argv = config.engine_argv(events_file=str(Path(temporary) / "bad.jsonl"))
            argv = [
                "--configHash=" + "0" * 64 if item.startswith("--configHash=") else item
                for item in argv
            ]
            bad = subprocess.run(
                [str(BINARY), *argv],
                cwd=ROOT,
                env=binary_environment(),
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(bad.returncode, 2)
            self.assertIn("configHash mismatch", bad.stderr)


if __name__ == "__main__":
    unittest.main()
