#!/usr/bin/env python3
"""Summarize observable results of one explicit ns-3 medium-access mode."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


PROFILE_LABELS = {
    "nominal": "nominal",
    "contention": "simultaneous_contention",
    "controlled_overload": "overload_characterization",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON object expected: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def mavlink_results(scenario: dict[str, Any]) -> dict[str, Any]:
    sequential = scenario.get("dual_uart_diagnostics", {}).get("sequential", {})
    detail: dict[str, Any] = {}
    for index in range(1, 6):
        name = f"uav{index}"
        row = sequential.get(name, {})
        control = row.get("control", {})
        payload = row.get("payload", {})
        detail[name] = {
            "system_id": row.get("system_id"),
            "control_heartbeat": bool(control.get("heartbeat")),
            "control_command_ack_sysid": control.get("ack_from_system_id"),
            "control_telemetry_after_command": bool(control.get("telemetry_after_command")),
            "payload_heartbeat": bool(payload.get("heartbeat")),
            "payload_telemetry": bool(payload.get("response_received")),
        }
    return {
        "five_real_sitl_system_ids": [detail[f"uav{index}"]["system_id"] for index in range(1, 6)],
        "per_uav": detail,
        "command_ack_count": len(scenario.get("command_acks", [])),
        "additional_data": scenario.get("additional_data", {}),
    }


def profile_metrics(profiles: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source_name, label in PROFILE_LABELS.items():
        profile = profiles.get(source_name)
        if not isinstance(profile, dict):
            result[label] = {"status": "unavailable"}
            continue
        classes = profile.get("classes", {})
        result[label] = {
            "source_profile": source_name,
            "offered": profile.get("offered", {}),
            "terminal_accounting": profile.get("terminal_accounting", {}),
            "classes": {
                name: {
                    "pdr": value.get("pdr"),
                    "delivered": value.get("packets_delivered_unique"),
                    "queue_enqueued": value.get("queue_enqueued_observed", {}),
                    "drops_at_ingress": value.get("dropped_at_ingress"),
                    "drops_in_medium": value.get("dropped_in_medium"),
                    "queue_latency_ms": value.get("latency_ms"),
                    "per_uav_delivered": value.get("per_uav_delivered_unique"),
                }
                for name, value in classes.items()
                if isinstance(value, dict)
            },
            "native_backoff_events": sum(
                1
                for event in events
                if event.get("event") == "backoff"
                and (event.get("packet_id") or "").startswith(source_name + ":")
            ),
            "native_retry_events": sum(
                1
                for event in events
                if event.get("event") == "backoff"
                and (event.get("packet_id") or "").startswith(source_name + ":")
            ),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    metrics = run_dir / "metrics"
    metadata = read_json(metrics / "medium_access_run.json")
    profiles = read_json(metrics / "traffic_profiles.json")
    scenario = read_json(metrics / "scenario_summary.json")
    stop = read_json(metrics / "ns3_stopped_probe.json")
    events = read_jsonl(run_dir / "logs/ns3_packet_events.jsonl")
    counts = Counter(str(event.get("event", "")) for event in events)
    queue_depths = [
        (str(event.get("device_id") or "unknown"), int(event["queue_depth_packets"]))
        for event in events
        if isinstance(event.get("queue_depth_packets"), int)
    ]
    maximum_queue_depth: dict[str, int] = {}
    for device, depth in queue_depths:
        maximum_queue_depth[device] = max(depth, maximum_queue_depth.get(device, 0))
    pcaps = sorted(path.name for path in (run_dir / "pcap").glob("*.pcap"))
    result = {
        "run_id": run_dir.name,
        "medium_access": metadata,
        "real_mavlink": mavlink_results(scenario),
        "profiles": profile_metrics(profiles, events),
        "native_events": {
            "backoff": counts["backoff"],
            "retries": counts["backoff"],
            "queue_drops": sum(1 for event in events if str(event.get("drop_reason") or "").startswith("queue_")),
            "phy_or_error_drops": sum(1 for event in events if event.get("event") == "drop" and not str(event.get("drop_reason") or "").startswith("queue_")),
            "sionna_fresh_events": sum(1 for event in events if event.get("radio_state_status") == "fresh"),
        },
        "native_queue_state": {
            "enqueue_events": counts["enqueue"],
            "dequeue_events": counts["dequeue"],
            "maximum_depth_packets_by_device": maximum_queue_depth,
            "maximum_depth_packets": max(maximum_queue_depth.values(), default=0),
        },
        "pcaps": {"count": len(pcaps), "files": pcaps},
        "ns3_stop": stop,
        "limitations": [
            (
                "This is the stock ns-3.40 CSMA shared-medium model, not a technology-specific customer radio modem."
                if metadata["medium_access_mode"] == "stock_ns3_csma"
                else "This is an explicit centralized project scheduling policy over an ns-3 CSMA channel, not stock CSMA or a technology-specific customer radio modem."
            ),
            "abstract_service_tier_v1 maps live Sionna RT state to packet errors; it is not a physical modem model.",
        ],
    }
    (metrics / "medium_access_baseline.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Native ns-3 CSMA baseline" if metadata["medium_access_mode"] == "stock_ns3_csma" else "# Centralized ns-3 CSMA regression",
        "",
        f"- Run: `{run_dir.name}`",
        f"- Mode: `{metadata['medium_access_mode']}`",
        f"- Real MAVLink system IDs: `{result['real_mavlink']['five_real_sitl_system_ids']}`",
        f"- Native backoff/retry events: `{counts['backoff']}/{counts['backoff']}`",
        f"- Maximum native queue depth: `{result['native_queue_state']['maximum_depth_packets']}` packets",
        f"- PCAP files: `{len(pcaps)}`; ns-3 stop broke exchange: `{stop.get('exchange_stopped')}`",
        "",
        "| Profile | Offered bit/s | Delivered | Backoff/retries |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, value in result["profiles"].items():
        offered = value.get("offered", {}).get("bits_per_second", "unavailable")
        terminal = value.get("terminal_accounting", {})
        lines.append(
            f"| {name} | {offered} | {terminal.get('delivered', 'unavailable')} | "
            f"{value.get('native_backoff_events', 'unavailable')}/{value.get('native_retry_events', 'unavailable')} |"
        )
    lines.extend(["", *result["limitations"], ""])
    (run_dir / "medium_access_baseline.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
