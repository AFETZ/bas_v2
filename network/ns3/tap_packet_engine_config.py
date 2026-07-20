#!/usr/bin/env python3
"""Resolve and hash the external ns-3 TapBridge packet-engine CLI contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "ams.tap_packet_engine/v1"
MAX_UAVS = 5
MAX_QUEUE_PACKETS = 1_000_000
MAX_DURATION_MS = 86_400_000
IFNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
DATA_RATE_RE = re.compile(r"^[1-9][0-9]*(?:bps|Kbps|Mbps|Gbps)$")
DELAY_RE = re.compile(r"^[1-9][0-9]*(?:ns|us|ms|s)$")


class ConfigError(ValueError):
    """The resolved engine configuration is unsafe or ambiguous."""


@dataclass(frozen=True)
class EngineConfig:
    """Every packet-behavior input covered by ``config_sha256``."""

    uav_count: int
    tap_gcs: str
    tap_uavs: tuple[str, ...]
    duration_ms: int
    radio_rate: str
    radio_delay: str
    queue_control_max_packets: int
    queue_payload_max_packets: int
    queue_additional_data_max_packets: int
    seed: int
    run: int
    event_epoch: int
    self_test: bool
    self_test_burst: int
    self_test_unknown_tos: bool
    sionna_ipc_enabled: bool = False
    sionna_state_file: str = ""
    sionna_poll_interval_ms: int = 1
    sionna_max_updates_per_poll: int = 64
    sionna_max_state_ttl_ms: int = 1000
    sionna_intervention: str = "natural"
    clock_datagram_socket: str = ""

    def validate(self) -> None:
        if not 1 <= self.uav_count <= MAX_UAVS:
            raise ConfigError(f"uav_count must be in 1..{MAX_UAVS}")
        if len(self.tap_uavs) != self.uav_count:
            raise ConfigError(
                f"tap_uavs must contain exactly {self.uav_count} names, "
                f"found {len(self.tap_uavs)}"
            )
        tap_names = (self.tap_gcs, *self.tap_uavs)
        invalid = [name for name in tap_names if not IFNAME_RE.fullmatch(name)]
        if invalid:
            raise ConfigError(f"invalid Linux TAP interface name(s): {invalid}")
        if len(set(tap_names)) != len(tap_names):
            raise ConfigError("TAP interface names must be unique")
        if not 1 <= self.duration_ms <= MAX_DURATION_MS:
            raise ConfigError(f"duration_ms must be in 1..{MAX_DURATION_MS}")
        if not DATA_RATE_RE.fullmatch(self.radio_rate):
            raise ConfigError("radio_rate must be a positive integral ns-3 data rate")
        if not DELAY_RE.fullmatch(self.radio_delay):
            raise ConfigError("radio_delay must be a positive integral ns-3 time")
        queue_limits = (
            self.queue_control_max_packets,
            self.queue_payload_max_packets,
            self.queue_additional_data_max_packets,
        )
        if any(not 1 <= value <= MAX_QUEUE_PACKETS for value in queue_limits):
            raise ConfigError(f"every queue bound must be in 1..{MAX_QUEUE_PACKETS}")
        if not 1 <= self.seed <= 0xFFFFFFFF:
            raise ConfigError("seed must be in 1..4294967295")
        if not 1 <= self.run <= 0x7FFFFFFFFFFFFFFF:
            raise ConfigError("run must be in 1..9223372036854775807")
        if not 1 <= self.event_epoch <= 0x7FFFFFFFFFFFFFFF:
            raise ConfigError("event_epoch must be a positive signed 64-bit integer")
        if not 1 <= self.self_test_burst <= 100_000:
            raise ConfigError("self_test_burst must be in 1..100000")
        if not self.self_test and self.self_test_unknown_tos:
            raise ConfigError("self_test_unknown_tos requires self_test")
        if self.sionna_ipc_enabled:
            if not self.sionna_state_file:
                raise ConfigError(
                    "sionna_state_file is required when Sionna IPC is enabled"
                )
            if not 1 <= self.sionna_poll_interval_ms <= 1000:
                raise ConfigError("sionna_poll_interval_ms must be in 1..1000")
            if not 1 <= self.sionna_max_updates_per_poll <= 4096:
                raise ConfigError("sionna_max_updates_per_poll must be in 1..4096")
            if not 1 <= self.sionna_max_state_ttl_ms <= 60000:
                raise ConfigError("sionna_max_state_ttl_ms must be in 1..60000")
            if self.sionna_intervention not in {
                "natural",
                "force_drop",
                "force_deliver",
            }:
                raise ConfigError("invalid sionna_intervention")
            if self.clock_datagram_socket and (
                not self.clock_datagram_socket.startswith("/")
                or len(self.clock_datagram_socket.encode()) >= 100
                or any(character in self.clock_datagram_socket for character in "\n\r\0")
            ):
                raise ConfigError("clock_datagram_socket must be a short absolute AF_UNIX path")

    def canonical_text(self) -> str:
        self.validate()
        fields = (
            ("contract", CONTRACT),
            ("uav_count", str(self.uav_count)),
            ("tap_gcs", self.tap_gcs),
            ("tap_uavs", ",".join(self.tap_uavs)),
            ("duration_ms", str(self.duration_ms)),
            ("radio_rate", self.radio_rate),
            ("radio_delay", self.radio_delay),
            ("queue_control_max_packets", str(self.queue_control_max_packets)),
            ("queue_payload_max_packets", str(self.queue_payload_max_packets)),
            (
                "queue_additional_data_max_packets",
                str(self.queue_additional_data_max_packets),
            ),
            ("seed", str(self.seed)),
            ("run", str(self.run)),
            ("event_epoch", str(self.event_epoch)),
            ("self_test", "1" if self.self_test else "0"),
            ("self_test_burst", str(self.self_test_burst)),
            ("self_test_unknown_tos", "1" if self.self_test_unknown_tos else "0"),
        )
        result = "".join(f"{key}={value}\n" for key, value in fields)
        if self.sionna_ipc_enabled:
            sionna_fields = (
                ("sionna_ipc_enabled", "1"),
                ("sionna_state_file", self.sionna_state_file),
                ("sionna_poll_interval_ms", str(self.sionna_poll_interval_ms)),
                (
                    "sionna_max_updates_per_poll",
                    str(self.sionna_max_updates_per_poll),
                ),
                ("sionna_max_state_ttl_ms", str(self.sionna_max_state_ttl_ms)),
                ("sionna_intervention", self.sionna_intervention),
            )
            result += "".join(f"{key}={value}\n" for key, value in sionna_fields)
            if self.clock_datagram_socket:
                result += f"clock_datagram_socket={self.clock_datagram_socket}\n"
        return result

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()

    def engine_argv(self, *, events_file: str, pcap_prefix: str = "") -> list[str]:
        self.validate()
        values: dict[str, object] = {
            "uavCount": self.uav_count,
            "tapGcs": self.tap_gcs,
            "tapUavs": ",".join(self.tap_uavs),
            "durationMs": self.duration_ms,
            "radioRate": self.radio_rate,
            "radioDelay": self.radio_delay,
            "queueControlMaxPackets": self.queue_control_max_packets,
            "queuePayloadMaxPackets": self.queue_payload_max_packets,
            "queueAdditionalDataMaxPackets": self.queue_additional_data_max_packets,
            "seed": self.seed,
            "run": self.run,
            "eventEpoch": self.event_epoch,
            "selfTest": int(self.self_test),
            "selfTestBurst": self.self_test_burst,
            "selfTestUnknownTos": int(self.self_test_unknown_tos),
            "configHash": self.sha256(),
            "eventsFile": events_file,
        }
        if pcap_prefix:
            values["pcapPrefix"] = pcap_prefix
        if self.sionna_ipc_enabled:
            values.update(
                {
                    "sionnaIpcEnabled": 1,
                    "sionnaStateFile": self.sionna_state_file,
                    "sionnaPollIntervalMs": self.sionna_poll_interval_ms,
                    "sionnaMaxUpdatesPerPoll": self.sionna_max_updates_per_poll,
                    "sionnaMaxStateTtlMs": self.sionna_max_state_ttl_ms,
                    "sionnaIntervention": self.sionna_intervention,
                }
            )
            if self.clock_datagram_socket:
                values["clockDatagramSocket"] = self.clock_datagram_socket
        return [f"--{key}={value}" for key, value in values.items()]


def _strict_mapping(path: Path) -> dict[str, Any]:
    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(
        loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConfigError(f"duplicate YAML key {key!r} in {path}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    if not path.is_file():
        raise ConfigError(f"missing configuration file: {path}")
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    if not isinstance(data, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return data


def from_repository(
    *,
    uav_count: int,
    duration_ms: int,
    seed: int,
    run: int,
    event_epoch: int,
    self_test: bool,
    self_test_burst: int,
    self_test_unknown_tos: bool,
    tap_gcs: str = "tap-gcs",
    tap_uavs: Sequence[str] | None = None,
    sionna_ipc_enabled: bool = False,
    sionna_state_file: str = "",
    sionna_poll_interval_ms: int = 1,
    sionna_max_updates_per_poll: int = 64,
    sionna_max_state_ttl_ms: int = 1000,
    sionna_intervention: str = "natural",
    clock_datagram_socket: str = "",
    endpoints_path: Path = ROOT / "network/config/endpoints.yaml",
    radio_path: Path = ROOT / "network/config/radio_24ghz.yaml",
) -> EngineConfig:
    endpoints = _strict_mapping(endpoints_path)
    radio = _strict_mapping(radio_path)
    uavs = endpoints.get("uavs")
    queues = endpoints.get("bridge", {}).get("queues", {})
    ns3 = radio.get("ns3", {})
    if not isinstance(uavs, list) or len(uavs) != MAX_UAVS:
        raise ConfigError("endpoints.yaml must define exactly five UAVs")
    expected_names = [f"uav{index}" for index in range(1, MAX_UAVS + 1)]
    if [item.get("name") for item in uavs if isinstance(item, dict)] != expected_names:
        raise ConfigError("endpoints.yaml UAV order/names must be exactly uav1..uav5")
    if tap_uavs is None:
        tap_uavs = (
            ("tap-uav",)
            if uav_count == 1
            else tuple(f"tap-uav{index}" for index in range(1, uav_count + 1))
        )
    try:
        config = EngineConfig(
            uav_count=uav_count,
            tap_gcs=tap_gcs,
            tap_uavs=tuple(tap_uavs),
            duration_ms=duration_ms,
            radio_rate=f"{int(ns3['channel_rate_bps'])}bps",
            radio_delay=f"{int(ns3['channel_delay_ms'])}ms",
            queue_control_max_packets=int(queues["control"]["max_packets"]),
            queue_payload_max_packets=int(queues["payload"]["max_packets"]),
            queue_additional_data_max_packets=int(
                queues["additional_data"]["max_packets"]
            ),
            seed=seed,
            run=run,
            event_epoch=event_epoch,
            self_test=self_test,
            self_test_burst=self_test_burst,
            self_test_unknown_tos=self_test_unknown_tos,
            sionna_ipc_enabled=sionna_ipc_enabled,
            sionna_state_file=sionna_state_file,
            sionna_poll_interval_ms=sionna_poll_interval_ms,
            sionna_max_updates_per_poll=sionna_max_updates_per_poll,
            sionna_max_state_ttl_ms=sionna_max_state_ttl_ms,
            sionna_intervention=sionna_intervention,
            clock_datagram_socket=clock_datagram_socket,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(
            f"invalid repository queue/radio configuration: {exc}"
        ) from exc
    config.validate()
    return config


def _parse_taps(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(value.split(","))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uav-count", type=int, required=True)
    parser.add_argument("--duration-ms", type=int, default=3_600_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--event-epoch", type=int, required=True)
    parser.add_argument("--tap-gcs", default="tap-gcs")
    parser.add_argument("--tap-uavs")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-burst", type=int, default=1)
    parser.add_argument("--self-test-unknown-tos", action="store_true")
    parser.add_argument("--sionna-ipc", action="store_true")
    parser.add_argument("--sionna-state-file", default="")
    parser.add_argument("--sionna-poll-interval-ms", type=int, default=1)
    parser.add_argument("--sionna-max-updates-per-poll", type=int, default=64)
    parser.add_argument("--sionna-max-state-ttl-ms", type=int, default=1000)
    parser.add_argument(
        "--sionna-intervention",
        choices=("natural", "force_drop", "force_deliver"),
        default="natural",
    )
    parser.add_argument("--clock-datagram-socket", default="")
    parser.add_argument("--events-file", default="ams-tap-packet-events.jsonl")
    parser.add_argument("--pcap-prefix", default="")
    parser.add_argument(
        "--endpoints", type=Path, default=ROOT / "network/config/endpoints.yaml"
    )
    parser.add_argument(
        "--radio", type=Path, default=ROOT / "network/config/radio_24ghz.yaml"
    )
    parser.add_argument("--print-hash", action="store_true")
    parser.add_argument("--print-argv", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    try:
        config = from_repository(
            uav_count=args.uav_count,
            duration_ms=args.duration_ms,
            seed=args.seed,
            run=args.run,
            event_epoch=args.event_epoch,
            self_test=args.self_test,
            self_test_burst=args.self_test_burst,
            self_test_unknown_tos=args.self_test_unknown_tos,
            sionna_ipc_enabled=args.sionna_ipc,
            sionna_state_file=args.sionna_state_file,
            sionna_poll_interval_ms=args.sionna_poll_interval_ms,
            sionna_max_updates_per_poll=args.sionna_max_updates_per_poll,
            sionna_max_state_ttl_ms=args.sionna_max_state_ttl_ms,
            sionna_intervention=args.sionna_intervention,
            clock_datagram_socket=args.clock_datagram_socket,
            tap_gcs=args.tap_gcs,
            tap_uavs=_parse_taps(args.tap_uavs),
            endpoints_path=args.endpoints,
            radio_path=args.radio,
        )
    except ConfigError as exc:
        parser.error(str(exc))

    payload = {
        "contract": CONTRACT,
        "config_sha256": config.sha256(),
        "canonical_config": config.canonical_text(),
        "resolved": {**asdict(config), "tap_uavs": list(config.tap_uavs)},
        "engine_argv": config.engine_argv(
            events_file=args.events_file, pcap_prefix=args.pcap_prefix
        ),
        "source_sha256": {
            str(args.endpoints): hashlib.sha256(
                args.endpoints.read_bytes()
            ).hexdigest(),
            str(args.radio): hashlib.sha256(args.radio.read_bytes()).hexdigest(),
        },
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.print_hash:
        print(payload["config_sha256"])
    elif args.print_argv:
        print("\n".join(payload["engine_argv"]))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
