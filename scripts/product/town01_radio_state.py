#!/usr/bin/env python3
"""Continuously map real Town01 Sionna link results into ns-3 packet states."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider.provider import ProviderError, query_tcp  # noqa: E402


TRAFFIC_CLASSES = ("control", "payload", "additional_data")


@dataclass(frozen=True)
class Town01RadioPolicy:
    """Validated provider radio values and shared-medium service tiers."""

    provider_radio: Mapping[str, float]
    channel_capacity_bps: int
    service_rates_bps: tuple[int, ...]


def compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _finite(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path} must be finite")
    return result


def _integer(value: object, path: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{path} must be an integer >= {minimum}")
    return value


def load_radio_policy(path: Path) -> Town01RadioPolicy:
    """Load capacity and provider-returned service rates from one product config."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load radio config {path}: {exc}") from exc
    root = _mapping(value, "radio config")
    radio = _mapping(root.get("radio"), "radio")
    provider_radio = {
        "carrier_hz": _finite(radio.get("carrier_hz"), "radio.carrier_hz"),
        "bandwidth_hz": _finite(
            radio.get("bandwidth_hz"), "radio.bandwidth_hz"
        ),
        "tx_power_dbm": _finite(radio.get("tx_power_dbm"), "radio.tx_power_dbm"),
    }
    if provider_radio["carrier_hz"] <= 0 or provider_radio["bandwidth_hz"] <= 0:
        raise ValueError("radio carrier and bandwidth must be positive")

    ns3 = _mapping(root.get("ns3"), "ns3")
    capacity = _integer(ns3.get("channel_rate_bps"), "ns3.channel_rate_bps", 1)
    raw_tiers = root.get("service_tier_selection")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise ValueError("service_tier_selection must be a non-empty list")
    rates: list[int] = []
    thresholds: list[float] = []
    for index, raw_tier in enumerate(raw_tiers):
        tier = _mapping(raw_tier, f"service_tier_selection[{index}]")
        thresholds.append(
            _finite(
                tier.get("min_sinr_db"),
                f"service_tier_selection[{index}].min_sinr_db",
            )
        )
        rates.append(
            _integer(
                tier.get("service_tier_bps"),
                f"service_tier_selection[{index}].service_tier_bps",
                0,
            )
        )
    if any(left <= right for left, right in zip(thresholds, thresholds[1:])):
        raise ValueError("service tier SINR thresholds must decrease strictly")
    if any(left <= right for left, right in zip(rates, rates[1:])):
        raise ValueError("service tier rates must decrease strictly")
    if rates[0] != capacity:
        raise ValueError("highest service tier must equal ns3.channel_rate_bps")
    if rates[-1] != 0:
        raise ValueError("lowest service tier must be zero")
    if any(rate > capacity for rate in rates):
        raise ValueError("service tier exceeds ns3.channel_rate_bps")
    return Town01RadioPolicy(provider_radio, capacity, tuple(rates))


def load_radio(path: Path) -> dict[str, float]:
    """Compatibility wrapper returning the provider request's radio object."""

    return dict(load_radio_policy(path).provider_radio)


def current_nodes(path: Path) -> list[dict[str, Any]]:
    state = load_json(path)
    nodes = state.get("nodes")
    if state.get("type") != "node_state" or not isinstance(nodes, list):
        raise ValueError("tracker state has the wrong type")
    by_id = {str(item.get("id")): item for item in nodes if isinstance(item, dict)}
    required = {"cp", *(f"uav{index}" for index in range(1, 6))}
    if set(by_id) != required:
        raise ValueError(f"tracker node set differs: {sorted(by_id)}")
    for node_id in required:
        node = by_id[node_id]
        position = node.get("position_m")
        if node.get("stale") or not isinstance(position, list) or len(position) != 3:
            raise ValueError(f"tracker node is stale or malformed: {node_id}")
    return [by_id["cp"], *(by_id[f"uav{index}"] for index in range(1, 6))]


def all_links() -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for index in range(1, 6):
        uav = f"uav{index}"
        for traffic_class in TRAFFIC_CLASSES:
            links.append({"tx": "cp", "rx": uav, "traffic_class": traffic_class})
            links.append({"tx": uav, "rx": "cp", "traffic_class": traffic_class})
    return links


def state_record(
    *,
    sequence: int,
    query_id: str,
    response_hash: str,
    link: dict[str, Any],
    applied_ns: int,
    ttl_ns: int,
    mapping_seed: int,
    service_rates_bps: Collection[int],
) -> dict[str, Any]:
    service_rate = int(link["service_tier_bps"])
    if service_rate not in service_rates_bps:
        raise ValueError(f"provider returned unsupported service tier: {service_rate}")
    directed_link = f"{link['tx']}>{link['rx']}"
    traffic_class = str(link["traffic_class"])
    state_id = hashlib.sha256(
        f"{query_id}|{directed_link}|{traffic_class}|{response_hash}".encode("utf-8")
    ).hexdigest()
    record: dict[str, Any] = {
        "schema": "ams.sionna.packet_state/v1",
        "state_sequence": sequence,
        "availability": "fresh",
        "directed_link": directed_link,
        "traffic_class": traffic_class,
        "query_id": query_id,
        "applied_state_id": state_id,
        "result_wire_sha256": response_hash,
        "validity_start_monotonic_ns": applied_ns,
        "expires_monotonic_ns": applied_ns + ttl_ns,
        "adapter_applied_monotonic_ns": applied_ns,
        "propagation_delay_ns": max(0, int(round(float(link["propagation_delay_ns"])))),
        "service_rate_bps": service_rate,
        "loss_probability": min(1.0, max(0.0, float(link["per_input"]))),
        "mapping_seed": mapping_seed,
        "mapping_version": "town01-provider-per-v1",
    }
    record["state_sha256"] = hashlib.sha256(compact(record).encode("utf-8")).hexdigest()
    return record


def append_states(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(compact(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def append_metrics(
    path: Path,
    *,
    query_index: int,
    query_started_ns: int,
    response: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    fields = [
        "query_index",
        "query_started_monotonic_ns",
        "provider_latency_ms",
        "tx",
        "rx",
        "traffic_class",
        "pathloss_db",
        "rssi_dbm",
        "sinr_db",
        "js_db",
        "service_tier_bps",
        "per_input",
        "link_state",
        "stale",
        "geometry_state",
        "path_count",
        "propagation_delay_ns",
    ]
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if new_file:
            writer.writeheader()
        for link in response["links"]:
            writer.writerow(
                {
                    "query_index": query_index,
                    "query_started_monotonic_ns": query_started_ns,
                    "provider_latency_ms": response.get("provider_latency_ms"),
                    **{field: link.get(field) for field in fields if field in link},
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-state", type=Path, required=True)
    parser.add_argument("--radio-config", type=Path, required=True)
    parser.add_argument("--state-output", type=Path, required=True)
    parser.add_argument("--metrics-output", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5090)
    parser.add_argument("--period-s", type=float, default=5.0)
    parser.add_argument("--ttl-s", type=float, default=20.0)
    parser.add_argument("--timeout-s", type=float, default=45.0)
    parser.add_argument("--mapping-seed", type=int, default=42)
    args = parser.parse_args()
    if not 0.5 <= args.period_s <= 30.0:
        raise SystemExit("--period-s must be in 0.5..30")
    if not args.period_s * 2 < args.ttl_s <= 60.0:
        raise SystemExit("--ttl-s must be greater than two periods and at most 60")

    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    radio_policy = load_radio_policy(args.radio_config)
    radio = dict(radio_policy.provider_radio)
    sequence = 0
    query_index = 0
    ttl_ns = int(args.ttl_s * 1e9)
    args.state_output.parent.mkdir(parents=True, exist_ok=True)
    args.state_output.write_text("", encoding="utf-8")
    args.ready_file.unlink(missing_ok=True)

    while not stop:
        cycle_started = time.monotonic()
        try:
            nodes = current_nodes(args.node_state)
            query_index += 1
            query_started_ns = time.monotonic_ns()
            query_id = f"town01-{query_index}-{query_started_ns}"
            request = {
                "type": "link_query",
                "time_s": time.time(),
                "deadline_ms": int(args.timeout_s * 1000),
                "radio": radio,
                "nodes": nodes,
                "emitters": [],
                "links": all_links(),
            }
            response = query_tcp(args.host, args.port, request, timeout_s=args.timeout_s)
            if response.get("type") != "link_state" or len(response.get("links", [])) != 30:
                raise ProviderError(f"provider returned incomplete state: {response!r}")
            if response.get("scene_id") != "cavise_town01_editor_lod0_full_20260712":
                raise ProviderError(f"provider scene differs: {response.get('scene_id')!r}")
            response_hash = hashlib.sha256(compact(response).encode("utf-8")).hexdigest()
            applied_ns = time.monotonic_ns()
            records = []
            for link in response["links"]:
                sequence += 1
                records.append(
                    state_record(
                        sequence=sequence,
                        query_id=query_id,
                        response_hash=response_hash,
                        link=link,
                        applied_ns=applied_ns,
                        ttl_ns=ttl_ns,
                        mapping_seed=args.mapping_seed,
                        service_rates_bps=radio_policy.service_rates_bps,
                    )
                )
            append_states(args.state_output, records)
            append_metrics(
                args.metrics_output,
                query_index=query_index,
                query_started_ns=query_started_ns,
                response=response,
            )
            if not args.ready_file.exists():
                args.ready_file.write_text(f"{query_id}\n", encoding="utf-8")
            print(
                f"RADIO_STATE query={query_index} links=30 "
                f"provider_latency_ms={response.get('provider_latency_ms')}",
                flush=True,
            )
        except (OSError, ValueError, json.JSONDecodeError, ProviderError) as exc:
            print(f"RADIO_STATE retry error={exc}", file=sys.stderr, flush=True)
        remaining = args.period_s - (time.monotonic() - cycle_started)
        deadline = time.monotonic() + max(remaining, 0.0)
        while not stop and time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
