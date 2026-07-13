#!/usr/bin/env python3
"""Build validation-facing artifacts from a sim_2_4ghz runtime run."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
TRAFFIC_CLASSES = ("control", "payload", "additional_data")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(temporary, path)


def normalize_sionna_log(run_dir: Path) -> tuple[bool, bool]:
    path = run_dir / "logs" / "sionna_link_queries.jsonl"
    has_query = False
    has_state = False
    if not path.is_file():
        return False, False
    for line in path.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        has_query = has_query or message.get("type") == "link_query"
        has_state = has_state or (message.get("type") == "link_state" and bool(message.get("links")))
    return has_query, has_state


def write_links_csv(run_dir: Path) -> list[dict[str, str]]:
    source = run_dir / "metrics" / "ns3_link_states.csv"
    if not source.is_file():
        raise FileNotFoundError(f"link-state source is missing: {source}")
    rows = read_csv(source)
    fieldnames = [
        "time_s",
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
        "source",
    ]
    write_rows(run_dir / "metrics" / "links.csv", fieldnames, rows)
    return rows


def write_queues_csv(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    bridge_log = run_dir / "logs" / "bridge.jsonl"
    if not bridge_log.is_file():
        raise FileNotFoundError(f"bridge source log is missing: {bridge_log}")
    if bridge_log.is_file():
        for line in bridge_log.read_text(errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            traffic_class = obj.get("traffic_class")
            if traffic_class not in TRAFFIC_CLASSES:
                continue
            queue_depth = obj.get("queue_depth")
            if isinstance(queue_depth, dict):
                depth = queue_depth.get(traffic_class, "")
            else:
                depth = obj.get("queue_depth_packets", "")
            rows.append(
                {
                    "time_s": obj.get("time_s") or obj.get("monotonic_ns") or "",
                    "event": obj.get("event", ""),
                    "traffic_class": traffic_class,
                    "priority": obj.get("priority", ""),
                    "queue_depth_packets": depth,
                    "bytes": obj.get("bytes") or obj.get("packet_bytes") or "",
                    "dropped": bool("drop" in str(obj.get("event", ""))),
                    "source": "bridge_queue_event",
                }
            )

    write_rows(
        run_dir / "metrics" / "queues.csv",
        ["time_s", "event", "traffic_class", "priority", "queue_depth_packets", "bytes", "dropped", "source"],
        rows,
    )
    return rows


def merge_summary(run_dir: Path, links: list[dict[str, str]], queue_rows: list[dict[str, Any]]) -> dict[str, Any]:
    runtime_path = run_dir / "metrics" / "runtime_summary.json"
    summary_path = run_dir / "metrics" / "summary.json"
    if runtime_path.is_file():
        summary = read_json(runtime_path)
    else:
        summary = read_json(summary_path)
        if summary.get("validation_engine") or summary.get("gates"):
            raise ValueError("refusing to use validator-owned summary.json as runtime input")
    has_query, has_state = normalize_sionna_log(run_dir)
    pcaps_ok = all(nonempty(run_dir / "pcap" / f"{name}.pcap") for name in TRAFFIC_CLASSES)
    heatmaps_ok = all(
        nonempty(run_dir / "heatmaps" / f"{name}.png")
        for name in ("rss", "sinr", "js", "degradation_zone", "service_tier")
    )
    flowmon_ok = nonempty(run_dir / "flowmon" / "flowmon.xml")

    per_values = [float(row.get("per_input") or 0.0) for row in links]
    js_values = [float(row.get("js_db") or -120.0) for row in links]
    sinr_values = [float(row.get("sinr_db") or 0.0) for row in links]
    service_values = [int(float(row.get("service_tier_bps") or 0)) for row in links]
    sources = {row.get("source") for row in links}
    class_events = {str(row.get("traffic_class")) for row in queue_rows}

    packets = summary.setdefault("packets", {})
    for traffic_class in TRAFFIC_CLASSES:
        packets.setdefault(f"{traffic_class if traffic_class != 'additional_data' else 'additional'}_tx", 0)
        packets.setdefault(f"{traffic_class if traffic_class != 'additional_data' else 'additional'}_rx", 0)

    # Post-processing is deliberately not an acceptance authority. These are
    # observations only; run_validation.sh independently evaluates raw proof.
    # In particular, file presence, varying PER, and configured class names must
    # never become self-reported P0 PASS flags here.
    summary["observations"] = {
        "class_pcaps_present": pcaps_ok,
        "flowmon_present": flowmon_ok,
        "heatmaps_present": heatmaps_ok,
        "sionna_query_present": has_query,
        "sionna_state_present": has_state,
        "sionna_sources": sorted(str(source) for source in sources if source),
        "link_rows": len(links),
        "queue_rows": len(queue_rows),
        "traffic_classes_observed_in_queue_log": sorted(class_events),
        "per_range_observed": bool(per_values and max(per_values) > min(per_values)),
        "positive_js_observed": bool(js_values and max(js_values) > 0.0),
        "low_sinr_observed": bool(sinr_values and min(sinr_values) < 6.0),
    }

    # Preserve producer claims only as explicitly untrusted observations. The
    # v2 validator never consumes these as acceptance evidence.
    producer_validation = summary.pop("validation", None)
    if isinstance(producer_validation, dict):
        summary["producer_observations"] = producer_validation

    # Post-processing always removes customer/P0 claims. The v2 validator
    # recomputes gate results from raw evidence.
    summary["p0_passed"] = False
    summary["customer_ready"] = False
    summary.pop("gates", None)

    summary["run_id"] = run_dir.name
    summary["scenario"] = "scenario_5uav"
    summary["uav_count"] = 5
    summary["traffic_classes"] = list(TRAFFIC_CLASSES)
    summary.setdefault("radio", {})
    if sinr_values:
        summary["radio"]["min_sinr_db"] = min(sinr_values)
    if js_values:
        summary["radio"]["max_js_db"] = max(js_values)
    summary["radio"]["late_sionna_queries"] = sum(1 for row in links if row.get("stale") == "true")
    summary["radio"]["service_tiers_observed_bps"] = sorted(set(service_values))

    if packets:
        loss_rate = summary.setdefault("loss_rate", {})
        for traffic_class, tx_key, rx_key in (
            ("control", "control_tx", "control_rx"),
            ("payload", "payload_tx", "payload_rx"),
            ("additional_data", "additional_tx", "additional_rx"),
        ):
            tx = float(packets.get(tx_key) or 0)
            rx = float(packets.get(rx_key) or 0)
            if tx <= 0 or rx < 0 or rx > tx:
                loss_rate[traffic_class] = None
                summary.setdefault("postprocess_diagnostics", []).append(
                    f"invalid packet counters for {traffic_class}: tx={tx:g} rx={rx:g}"
                )
            else:
                loss_rate[traffic_class] = (tx - rx) / tx

    # Preserve producer/derived runtime metrics separately. Validation owns the
    # customer-facing summary.json after this step.
    write_json(runtime_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if (run_dir / "metrics/evidence_manifest.json").exists():
        print("FAIL refusing to postprocess a sealed raw-evidence run", file=sys.stderr)
        return 2
    try:
        links = write_links_csv(run_dir)
        queue_rows = write_queues_csv(run_dir)
        summary = merge_summary(run_dir, links, queue_rows)
    except Exception as exc:
        print(f"FAIL postprocess: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"run_dir": str(run_dir), "observations": summary.get("observations", {})}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
