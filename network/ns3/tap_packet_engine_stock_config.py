#!/usr/bin/env python3
"""Resolve the public-API-only ns-3.40 CSMA baseline launch contract."""

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
CONTRACT = "ams.tap_packet_engine.stock/v1"
MODE = "stock_ns3_csma"
MAX_UAVS = 5
IFNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
RATE_RE = re.compile(r"^[1-9][0-9]*bps$")


class ConfigError(ValueError):
    """The stock engine configuration is unsafe or ambiguous."""


def strict_yaml(path: Path) -> dict[str, Any]:
    class UniqueLoader(yaml.SafeLoader):
        pass

    def construct_mapping(
        loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ConfigError(f"duplicate YAML key {key!r} in {path}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    if not path.is_file():
        raise ConfigError(f"missing configuration file: {path}")
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return value


@dataclass(frozen=True)
class StockConfig:
    uav_count: int
    tap_gcs: str
    tap_uavs: tuple[str, ...]
    duration_ms: int
    radio_rate: str
    radio_delay: str
    queue_max_packets: int
    control_tos: int
    payload_tos: int
    additional_data_tos: int
    seed: int
    run: int
    event_epoch: int
    self_test: bool
    self_test_burst: int
    sionna_ipc_enabled: bool
    sionna_state_file: str
    sionna_poll_interval_ms: int
    sionna_max_state_ttl_ms: int
    medium_access_mode: str = MODE
    ns3_source_version: str = "3.40"
    ns3_tree_kind: str = "pristine_git_checkout"
    upstream_patch_applied: bool = False
    global_scheduler_enabled: bool = False
    ingress_shaping_enabled: bool = False
    radio_mapping_mode: str = "abstract_service_tier_v1"
    propagation_backend: str = "sionna_rt"

    def validate(self) -> None:
        if self.medium_access_mode != MODE:
            raise ConfigError("the stock target only accepts stock_ns3_csma")
        if not 1 <= self.uav_count <= MAX_UAVS or len(self.tap_uavs) != self.uav_count:
            raise ConfigError("uav_count and tap_uavs must describe one to five UAVs")
        if not all(IFNAME_RE.fullmatch(name) for name in (self.tap_gcs, *self.tap_uavs)):
            raise ConfigError("TAP interface names are invalid")
        if len({self.tap_gcs, *self.tap_uavs}) != self.uav_count + 1:
            raise ConfigError("TAP interface names must be unique")
        if not 1 <= self.duration_ms <= 86_400_000:
            raise ConfigError("duration_ms must be in 1..86400000")
        if not RATE_RE.fullmatch(self.radio_rate) or self.queue_max_packets != 100:
            raise ConfigError("stock_ns3_csma uses the upstream default 100-packet CSMA queue")
        if not re.fullmatch(r"[1-9][0-9]*(?:ns|us|ms|s)", self.radio_delay):
            raise ConfigError("radio_delay must be an ns-3 time value")
        if any(not 0 <= value <= 255 for value in (
            self.control_tos, self.payload_tos, self.additional_data_tos
        )) or len({self.control_tos, self.payload_tos, self.additional_data_tos}) != 3:
            raise ConfigError("traffic-class TOS values must be distinct bytes")
        if not 1 <= self.seed <= 0xFFFFFFFF or self.run < 1 or self.event_epoch < 1:
            raise ConfigError("seed, run, and event_epoch must be positive")
        if not 1 <= self.self_test_burst <= 100_000:
            raise ConfigError("self_test_burst must be in 1..100000")
        if self.sionna_ipc_enabled and (
            not self.sionna_state_file
            or not 1 <= self.sionna_poll_interval_ms <= 1000
            or not 1 <= self.sionna_max_state_ttl_ms <= 60_000
        ):
            raise ConfigError("Sionna IPC settings are invalid")
        if any((
            self.upstream_patch_applied,
            self.global_scheduler_enabled,
            self.ingress_shaping_enabled,
        )):
            raise ConfigError("stock_ns3_csma cannot enable a project packet policy")
        if self.radio_mapping_mode != "abstract_service_tier_v1":
            raise ConfigError("the stock baseline uses abstract_service_tier_v1")
        if self.propagation_backend != "sionna_rt":
            raise ConfigError("the stock baseline requires live sionna_rt")

    def canonical_text(self) -> str:
        self.validate()
        return "".join(
            f"{key}={value}\n"
            for key, value in (
                ("contract", CONTRACT),
                ("uav_count", self.uav_count),
                ("tap_gcs", self.tap_gcs),
                ("tap_uavs", ",".join(self.tap_uavs)),
                ("duration_ms", self.duration_ms),
                ("radio_rate", self.radio_rate),
                ("radio_delay", self.radio_delay),
                ("queue_max_packets", self.queue_max_packets),
                ("control_tos", self.control_tos),
                ("payload_tos", self.payload_tos),
                ("additional_data_tos", self.additional_data_tos),
                ("seed", self.seed),
                ("run", self.run),
                ("event_epoch", self.event_epoch),
                ("self_test", int(self.self_test)),
                ("self_test_burst", self.self_test_burst),
                ("sionna_ipc_enabled", int(self.sionna_ipc_enabled)),
                ("sionna_state_file", self.sionna_state_file),
                ("sionna_poll_interval_ms", self.sionna_poll_interval_ms),
                ("sionna_max_state_ttl_ms", self.sionna_max_state_ttl_ms),
                ("medium_access_mode", self.medium_access_mode),
                ("ns3_source_version", self.ns3_source_version),
                ("ns3_tree_kind", self.ns3_tree_kind),
                ("upstream_patch_applied", int(self.upstream_patch_applied)),
                ("global_scheduler_enabled", int(self.global_scheduler_enabled)),
                ("ingress_shaping_enabled", int(self.ingress_shaping_enabled)),
                ("radio_mapping_mode", self.radio_mapping_mode),
                ("propagation_backend", self.propagation_backend),
            )
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_text().encode()).hexdigest()

    def engine_argv(self, *, events_file: str, pcap_prefix: str) -> list[str]:
        self.validate()
        values: dict[str, str | int] = {
            "uavCount": self.uav_count,
            "tapGcs": self.tap_gcs,
            "tapUavs": ",".join(self.tap_uavs),
            "durationMs": self.duration_ms,
            "radioRate": self.radio_rate,
            "radioDelay": self.radio_delay,
            "queueMaxPackets": self.queue_max_packets,
            "controlTos": self.control_tos,
            "payloadTos": self.payload_tos,
            "additionalDataTos": self.additional_data_tos,
            "seed": self.seed,
            "run": self.run,
            "eventEpoch": self.event_epoch,
            "selfTest": int(self.self_test),
            "selfTestBurst": self.self_test_burst,
            "configHash": self.sha256(),
            "eventsFile": events_file,
            "pcapPrefix": pcap_prefix,
        }
        if self.sionna_ipc_enabled:
            values.update(
                {
                    "sionnaIpcEnabled": 1,
                    "sionnaStateFile": self.sionna_state_file,
                    "sionnaPollIntervalMs": self.sionna_poll_interval_ms,
                    "sionnaMaxStateTtlMs": self.sionna_max_state_ttl_ms,
                }
            )
        return [f"--{key}={value}" for key, value in values.items()]


def from_repository(
    *,
    uav_count: int,
    duration_ms: int,
    seed: int,
    run: int,
    event_epoch: int,
    self_test: bool,
    self_test_burst: int,
    tap_gcs: str,
    tap_uavs: Sequence[str] | None,
    sionna_ipc_enabled: bool,
    sionna_state_file: str,
    sionna_poll_interval_ms: int,
    sionna_max_state_ttl_ms: int | None,
    medium_access_mode: str,
    endpoints_path: Path,
    radio_path: Path,
    qos_path: Path,
) -> StockConfig:
    endpoints = strict_yaml(endpoints_path)
    radio = strict_yaml(radio_path)
    qos = strict_yaml(qos_path)
    if medium_access_mode != MODE:
        raise ConfigError("stock launcher received a non-stock medium access mode")
    configured_mode = qos.get("medium_access", {}).get("mode")
    allowed = qos.get("medium_access", {}).get("supported_modes")
    if configured_mode not in {MODE, "centralized_priority_scheduler_over_csma_channel"}:
        raise ConfigError("product medium_access.mode is invalid")
    if not isinstance(allowed, list) or set(allowed) != {
        MODE, "centralized_priority_scheduler_over_csma_channel"
    }:
        raise ConfigError("product medium_access.supported_modes is invalid")
    if not isinstance(endpoints.get("uavs"), list) or len(endpoints["uavs"]) != MAX_UAVS:
        raise ConfigError("endpoints.yaml must define five UAVs")
    if tap_uavs is None:
        tap_uavs = tuple(
            "tap-uav" if uav_count == 1 else f"tap-uav{index}"
            for index in range(1, uav_count + 1)
        )
    try:
        ns3 = radio["ns3"]
        classes = qos["classes"]
        medium = qos["medium_access"]
        config = StockConfig(
            uav_count=uav_count,
            tap_gcs=tap_gcs,
            tap_uavs=tuple(tap_uavs),
            duration_ms=duration_ms,
            radio_rate=f"{int(ns3['channel_rate_bps'])}bps",
            radio_delay=f"{int(ns3['channel_delay_ms'])}ms",
            queue_max_packets=int(ns3["queue_max_packets"]),
            control_tos=int(classes["control"]["tos"]),
            payload_tos=int(classes["payload"]["tos"]),
            additional_data_tos=int(classes["additional_data"]["tos"]),
            seed=seed,
            run=run,
            event_epoch=event_epoch,
            self_test=self_test,
            self_test_burst=self_test_burst,
            sionna_ipc_enabled=sionna_ipc_enabled,
            sionna_state_file=sionna_state_file,
            sionna_poll_interval_ms=sionna_poll_interval_ms,
            sionna_max_state_ttl_ms=int(
                medium.get("sionna_max_state_ttl_ms", qos["channel_state"]["maximum_age_ms"])
                if sionna_max_state_ttl_ms is None
                else sionna_max_state_ttl_ms
            ),
            radio_mapping_mode=str(medium["radio_mapping_mode"]),
            propagation_backend=str(medium["propagation_backend"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid product configuration: {exc}") from exc
    config.validate()
    return config


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
    parser.add_argument("--sionna-ipc", action="store_true")
    parser.add_argument("--sionna-state-file", default="")
    parser.add_argument("--sionna-poll-interval-ms", type=int, default=1)
    parser.add_argument("--sionna-max-state-ttl-ms", type=int)
    parser.add_argument("--medium-access-mode", default=MODE)
    parser.add_argument("--events-file", default="ams-tap-packet-events.jsonl")
    parser.add_argument("--pcap-prefix", default="")
    parser.add_argument("--endpoints", type=Path, default=ROOT / "network/config/endpoints.yaml")
    parser.add_argument("--radio", type=Path, default=ROOT / "network/config/radio_24ghz.yaml")
    parser.add_argument("--qos", type=Path, default=ROOT / "network/config/communication_qos.yaml")
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
            tap_gcs=args.tap_gcs,
            tap_uavs=tuple(args.tap_uavs.split(",")) if args.tap_uavs else None,
            sionna_ipc_enabled=args.sionna_ipc,
            sionna_state_file=args.sionna_state_file,
            sionna_poll_interval_ms=args.sionna_poll_interval_ms,
            sionna_max_state_ttl_ms=args.sionna_max_state_ttl_ms,
            medium_access_mode=args.medium_access_mode,
            endpoints_path=args.endpoints,
            radio_path=args.radio,
            qos_path=args.qos,
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
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (args.endpoints, args.radio, args.qos)
        },
    }
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if args.print_argv:
        print("\n".join(payload["engine_argv"]))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
