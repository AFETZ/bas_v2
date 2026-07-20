#!/usr/bin/env python3
"""Continuously query Sionna for one link and write a live SINR trace."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.radio_provider.provider import (  # noqa: E402
    DEFAULT_JAMMERS,
    DEFAULT_RADIO,
    DEFAULT_SCENARIO,
    ProviderError,
    compact_json,
    emitters_from_config,
    load_yaml,
    nodes_from_scenario,
    query_tcp,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ProviderError(f"JSON file must contain an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_node_state(path: Path, required: bool) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise ProviderError(f"node-state file does not exist yet: {path}")
        return None
    state = read_json(path)
    if state.get("type") != "node_state":
        raise ProviderError(f"node-state file has unexpected type: {state.get('type')!r}")
    if not isinstance(state.get("nodes"), list) or not state["nodes"]:
        raise ProviderError("node-state file has no nodes")
    return state


def require_fresh_ros_link_nodes(state: dict[str, Any], tx: str, rx: str) -> None:
    nodes = {str(node.get("id")): node for node in state.get("nodes", []) if isinstance(node, dict)}
    for node_id in (tx, rx):
        node = nodes.get(node_id)
        if node is None:
            raise ProviderError(f"source=ros requires node {node_id} in node-state")
        source_topic = str(node.get("source_topic", ""))
        if bool(node.get("stale")) or source_topic.startswith("fallback:"):
            raise ProviderError(f"source=ros has no fresh odometry for {node_id}: {source_topic}")


def replay_state(
    scenario: dict[str, Any],
    jammers: dict[str, Any],
    elapsed_s: float,
    moving_node: str,
    amplitude_m: float,
    period_s: float,
) -> dict[str, Any]:
    nodes = nodes_from_scenario(scenario)
    phase = 2.0 * math.pi * (elapsed_s / max(period_s, 0.1))
    offset = amplitude_m * 0.5 * (1.0 - math.cos(phase))
    for node in nodes:
        if node["id"] == moving_node:
            position = list(node["position_m"])
            position[0] = float(position[0]) + offset
            node["position_m"] = position
            node["source_topic"] = "replay:range_sweep"
            break
    return {
        "type": "node_state",
        "time_s": time.time(),
        "wall_time": utc_now(),
        "source": "replay",
        "nodes": nodes,
        "emitters": emitters_from_config(jammers, enabled_only=True),
        "missing_nodes": [],
        "stale_nodes": [],
    }


def request_from_state(
    state: dict[str, Any],
    radio_cfg: dict[str, Any],
    tx: str,
    rx: str,
    traffic_class: str,
    deadline_ms: int,
    bidirectional: bool,
) -> dict[str, Any]:
    radio = dict(radio_cfg.get("radio", {}))
    links = [{"tx": tx, "rx": rx, "traffic_class": traffic_class}]
    if bidirectional:
        links.append({"tx": rx, "rx": tx, "traffic_class": traffic_class})
    return {
        "type": "link_query",
        "time_s": time.time(),
        "deadline_ms": int(deadline_ms),
        "radio": {
            "carrier_hz": float(radio.get("carrier_hz", 2.4e9)),
            "bandwidth_hz": float(radio.get("bandwidth_hz", 1e6)),
            "tx_power_dbm": float(radio.get("tx_power_dbm", 33.0)),
        },
        "nodes": state["nodes"],
        "emitters": state.get("emitters", []),
        "links": links,
    }


def find_link(response: dict[str, Any], tx: str, rx: str, traffic_class: str) -> dict[str, Any]:
    if response.get("type") != "link_state":
        raise ProviderError(f"provider returned {response.get('type')}: {response.get('error', response)!r}")
    for link in response.get("links", []):
        if (
            str(link.get("tx")) == tx
            and str(link.get("rx")) == rx
            and str(link.get("traffic_class")) == traffic_class
        ):
            return dict(link)
    raise ProviderError(f"provider response did not contain link {tx}->{rx}/{traffic_class}")


def write_plot(path: Path, samples: list[dict[str, Any]], tx: str, rx: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore

    path.parent.mkdir(parents=True, exist_ok=True)
    x = [float(sample["elapsed_s"]) for sample in samples]
    y = [float(sample["sinr_db"]) for sample in samples]

    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.plot(x, y, color="#0f766e", linewidth=2.0)
    ax.scatter(x[-1:], y[-1:], color="#b91c1c", s=28, zorder=3)
    ax.set_xlabel("elapsed (s)")
    ax.set_ylabel("SINR (dB)")
    ax.set_title(f"Live SINR {tx} -> {rx}")
    ax.grid(True, color="#d1d5db", linewidth=0.7)
    fig.tight_layout()
    temp = path.with_suffix(path.suffix + ".tmp")
    fig.savefig(temp, dpi=140, format="png")
    plt.close(fig)
    temp.replace(path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(compact_json(value) + "\n")


def open_csv(path: Path) -> tuple[Any, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("w", encoding="utf-8", newline="")
    fieldnames = [
        "elapsed_s",
        "wall_time",
        "source",
        "tx",
        "rx",
        "traffic_class",
        "sinr_db",
        "rssi_dbm",
        "pathloss_db",
        "js_db",
        "service_tier_bps",
        "per_input",
        "link_state",
        "stale",
        "provider_latency_ms",
    ]
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    stream.flush()
    return stream, writer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument("--radio-config", default=str(DEFAULT_RADIO))
    parser.add_argument("--jammers-config", default=str(DEFAULT_JAMMERS))
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--node-state", default=None)
    parser.add_argument("--source", choices=["auto", "ros", "replay"], default="auto")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5090)
    parser.add_argument("--tx", default="uav1")
    parser.add_argument("--rx", default="uav2")
    parser.add_argument("--traffic-class", default="control")
    parser.add_argument("--bidirectional", action="store_true")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--rate-hz", type=float, default=1.0)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--timeout-s", type=float, default=None)
    parser.add_argument("--startup-timeout-s", type=float, default=10.0)
    parser.add_argument("--plot-every-s", type=float, default=1.0)
    parser.add_argument("--replay-moving-node", default=None)
    parser.add_argument("--replay-amplitude-m", type=float, default=1600.0)
    parser.add_argument("--replay-period-s", type=float, default=20.0)
    parser.add_argument("--no-plot", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = Path(args.run_dir).resolve() if args.run_dir else ROOT_DIR / "runs" / datetime.now(timezone.utc).strftime("live_sinr_%Y%m%dT%H%M%SZ")
    node_state_path = Path(args.node_state).resolve() if args.node_state else run_dir / "logs/node_state.json"
    csv_path = run_dir / "metrics/live_sinr.csv"
    plot_path = run_dir / "plots/live_sinr.png"
    log_path = run_dir / "logs/live_sinr_queries.jsonl"
    summary_path = run_dir / "metrics/live_sinr_summary.json"

    scenario = load_yaml(Path(args.scenario))
    radio_cfg = load_yaml(Path(args.radio_config))
    jammers = load_yaml(Path(args.jammers_config))
    moving_node = args.replay_moving_node or args.rx
    timeout_s = float(args.timeout_s if args.timeout_s is not None else max(args.deadline_ms / 1000.0, 1.0))
    period_s = 1.0 / max(float(args.rate_hz), 0.001)
    duration_s = float(args.duration)
    started = time.monotonic()
    last_plot = -1.0
    samples: list[dict[str, Any]] = []

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "metrics").mkdir(exist_ok=True)
    (run_dir / "plots").mkdir(exist_ok=True)

    csv_stream, csv_writer = open_csv(csv_path)
    try:
        while True:
            elapsed = time.monotonic() - started
            if duration_s > 0 and elapsed > duration_s and samples:
                break

            state: dict[str, Any] | None = None
            if args.source in ("auto", "ros"):
                deadline = started + float(args.startup_timeout_s)
                required = args.source == "ros" or time.monotonic() < deadline
                try:
                    state = load_node_state(node_state_path, required=required)
                    if args.source == "auto" and state is not None and state.get("source") == "replay":
                        state = None
                    if args.source == "ros" and state is not None:
                        require_fresh_ros_link_nodes(state, args.tx, args.rx)
                except ProviderError:
                    if args.source == "ros":
                        raise
                    state = None
            if state is None:
                state = replay_state(
                    scenario,
                    jammers,
                    elapsed,
                    moving_node=moving_node,
                    amplitude_m=float(args.replay_amplitude_m),
                    period_s=float(args.replay_period_s),
                )
                write_json(node_state_path, state)

            request = request_from_state(
                state,
                radio_cfg,
                tx=args.tx,
                rx=args.rx,
                traffic_class=args.traffic_class,
                deadline_ms=int(args.deadline_ms),
                bidirectional=bool(args.bidirectional),
            )
            response = query_tcp(args.host, int(args.port), request, timeout_s=timeout_s)
            link = find_link(response, args.tx, args.rx, args.traffic_class)
            source = str(state.get("source", "unknown"))
            sample = {
                "elapsed_s": round(elapsed, 3),
                "wall_time": utc_now(),
                "source": source,
                "tx": args.tx,
                "rx": args.rx,
                "traffic_class": args.traffic_class,
                "sinr_db": float(link.get("sinr_db", 0.0)),
                "rssi_dbm": float(link.get("rssi_dbm", 0.0)),
                "pathloss_db": float(link.get("pathloss_db", 0.0)),
                "js_db": float(link.get("js_db", 0.0)),
                "service_tier_bps": int(link.get("service_tier_bps", 0)),
                "per_input": float(link.get("per_input", 0.0)),
                "link_state": str(link.get("link_state", "unknown")),
                "stale": bool(link.get("stale", False)),
                "provider_latency_ms": float(response.get("provider_latency_ms", 0.0)),
            }
            samples.append(sample)
            csv_writer.writerow(sample)
            csv_stream.flush()
            append_jsonl(log_path, {"request": request, "response": response, "sample": sample})

            if not args.no_plot and (elapsed - last_plot >= float(args.plot_every_s) or len(samples) == 1):
                write_plot(plot_path, samples, args.tx, args.rx)
                last_plot = elapsed

            print(
                "LIVE_SINR "
                f"t={sample['elapsed_s']:.3f}s "
                f"{args.tx}->{args.rx}/{args.traffic_class} "
                f"sinr={sample['sinr_db']:.3f}dB "
                f"state={sample['link_state']} "
                f"source={source}",
                flush=True,
            )
            sleep_s = period_s - ((time.monotonic() - started) - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)
    finally:
        csv_stream.close()

    if samples and not args.no_plot:
        write_plot(plot_path, samples, args.tx, args.rx)

    sinrs = [float(sample["sinr_db"]) for sample in samples]
    summary = {
        "type": "live_sinr_summary",
        "samples": len(samples),
        "tx": args.tx,
        "rx": args.rx,
        "traffic_class": args.traffic_class,
        "sources": sorted({str(sample["source"]) for sample in samples}),
        "min_sinr_db": min(sinrs) if sinrs else None,
        "max_sinr_db": max(sinrs) if sinrs else None,
        "stale_samples": sum(1 for sample in samples if sample["stale"]),
        "csv": str(csv_path.relative_to(run_dir)),
        "plot": str(plot_path.relative_to(run_dir)) if not args.no_plot else None,
        "query_log": str(log_path.relative_to(run_dir)),
    }
    write_json(summary_path, summary)
    print(compact_json(summary))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProviderError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
