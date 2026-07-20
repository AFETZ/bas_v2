#!/usr/bin/env python3
"""Fail-closed manual GCS bridge for the rock radio demo.

This helper gives a human-operated GCS a bridge endpoint instead of direct
SITL master ports. It mirrors every manual MAVLink byte chunk into the
configured priority UDP bridge ingress and gates forwarding to the internal
SITL TCP tail on fresh ns-3/Sionna link evidence.

The direct SITL TCP connection is intentionally treated as UAV-side plumbing,
not as an advertised or P0-eligible operator endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import signal
import socket
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - covered by shell preflight.
    yaml = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINTS = ROOT_DIR / "network" / "config" / "endpoints.yaml"
PORT_KEYS = {
    "control": "control_udp",
    "payload": "payload_udp",
    "additional_data": "additional_data_udp",
}


@dataclass
class TraceStatus:
    ok: bool
    reason: str
    path: str
    mtime_age_s: float | None = None
    snr_db: float | None = None
    rssi_dbm: float | None = None
    source: str | None = None
    tx_stale: bool | None = None
    rx_stale: bool | None = None


@dataclass
class BridgeProof:
    ok: bool
    reason: str
    endpoint: str
    path: str


class ManualGcsBridge:
    def __init__(self, args: argparse.Namespace, config: dict[str, Any]):
        self.args = args
        self.config = config
        self.uav = self._find_uav(args.uav)
        self.manual = config.get("bridge", {}).get("ground_control", {}).get("manual_gcs", {})
        self.traffic_class = args.traffic_class or str(self.manual.get("traffic_class", "control"))
        if self.traffic_class not in PORT_KEYS:
            raise ValueError(f"unsupported traffic class for manual GCS bridge: {self.traffic_class}")

        self.bind_host = args.bind_host or str(self.manual.get("bind_host", "127.0.0.1"))
        self.bind_port = int(args.bind_port or self.manual.get("tcp_port", 14600))
        self.require_modeled_path = args.require_modeled_path
        self.max_trace_age_s = float(args.max_trace_age_s)
        self.link_down_snr_db = float(args.link_down_snr_db)
        self.startup_timeout_s = float(args.startup_timeout_s)

        run_dir = args.run_dir
        self.log_path = args.log or run_dir / str(self.manual.get("log", "logs/manual_gcs_bridge.jsonl"))
        self.summary_path = args.summary or run_dir / str(
            self.manual.get("summary", "metrics/manual_gcs_bridge_summary.json")
        )
        self.bridge_log = args.bridge_log or run_dir / "logs/bridge.jsonl"
        self.ns3_trace = args.ns3_trace or run_dir / str(
            self.manual.get("ns3_trace", "metrics/ns3_sionna_rt_live.csv")
        )

        self.bridge_host = str(config["bridge"]["ground_control"]["bind_host"])
        self.bridge_port = int(self.uav["bridge_ports"][PORT_KEYS[self.traffic_class]])
        self.sitl_host = str(self.uav["direct_sitl"]["master_tcp"]["host"])
        self.sitl_port = int(self.uav["direct_sitl"]["master_tcp"]["port"])
        self.gcs_endpoint = f"tcp:{self.bind_host}:{self.bind_port}"
        self.stop_event: asyncio.Event | None = None
        self.started_at = time.time()
        self.stats: dict[str, Any] = {
            "connections": 0,
            "gcs_to_sitl_packets": 0,
            "sitl_to_gcs_packets": 0,
            "mirrored_packets": 0,
            "dropped_packets": 0,
            "bytes_gcs_to_sitl": 0,
            "bytes_sitl_to_gcs": 0,
            "last_drop_reason": None,
            "last_trace": None,
            "last_bridge_proof": None,
        }

    def _find_uav(self, name: str) -> dict[str, Any]:
        for uav in self.config.get("uavs", []):
            if uav.get("name") == name:
                return uav
        raise ValueError(f"unknown UAV in endpoint config: {name}")

    def dry_run_record(self) -> dict[str, Any]:
        bridge_proof = self.check_bridge_bind()
        trace = read_latest_trace(self.ns3_trace, self.max_trace_age_s, self.link_down_snr_db)
        return {
            "gcs_endpoint": self.gcs_endpoint,
            "uav": self.uav["name"],
            "system_id": self.uav["system_id"],
            "traffic_class": self.traffic_class,
            "bridge_ingress": f"udp:{self.bridge_host}:{self.bridge_port}",
            "internal_sitl_tail": f"tcp:{self.sitl_host}:{self.sitl_port}",
            "require_modeled_path": self.require_modeled_path,
            "direct_sitl_operator_endpoint": False,
            "direct_sitl_p0_eligible": False,
            "bridge_bind": asdict(bridge_proof),
            "ns3_trace": asdict(trace),
            "guard_only_mode": True,
            "p0_packet_path_eligible": False,
            "p0_no_bypass_claim_allowed": False,
            "p0_guard": (
                "Manual GCS direct SITL ports are not advertised to the operator. "
                "This guard-only helper mirrors bytes into bridge ingress but does not yet prove "
                "a full bidirectional ns-3 egress command path."
            ),
        }

    def emit(self, event: str, **fields: Any) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time_s": time.time(),
            "event": event,
            "run_id": self.args.run_id,
            "gcs_endpoint": self.gcs_endpoint,
            "uav": self.uav["name"],
            "traffic_class": self.traffic_class,
            **fields,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def write_summary(self) -> None:
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        bridge_proof = self.check_bridge_bind()
        trace = read_latest_trace(self.ns3_trace, self.max_trace_age_s, self.link_down_snr_db)
        mirrored = int(self.stats["mirrored_packets"])
        guard_satisfied = (
            self.require_modeled_path
            and bridge_proof.ok
            and trace.ok
            and mirrored > 0
        )
        summary = {
            "run_id": self.args.run_id,
            "gcs_endpoint": self.gcs_endpoint,
            "uav": self.uav["name"],
            "system_id": self.uav["system_id"],
            "traffic_class": self.traffic_class,
            "require_modeled_path": self.require_modeled_path,
            "bridge_ingress": f"udp:{self.bridge_host}:{self.bridge_port}",
            "internal_sitl_tail": f"tcp:{self.sitl_host}:{self.sitl_port}",
            "direct_sitl_operator_endpoint": False,
            "direct_sitl_p0_eligible": False,
            "bridge_bind": asdict(bridge_proof),
            "ns3_trace": asdict(trace),
            "guard_only_mode": True,
            "manual_bridge_guard_satisfied": guard_satisfied,
            "p0_packet_path_eligible": False,
            "p0_no_bypass_claim_allowed": False,
            "p0_guard_reason": (
                "guard satisfied: manual bytes were mirrored into modeled bridge ingress and direct SITL was not advertised, but full bidirectional ns-3 egress remains future work"
                if guard_satisfied
                else "guard not satisfied until bridge bind, fresh ns-3/Sionna trace, and mirrored manual packets are all present"
            ),
            "duration_s": time.time() - self.started_at,
            **self.stats,
        }
        self.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def check_bridge_bind(self) -> BridgeProof:
        endpoint = f"{self.uav['name']}_{self.traffic_class}"
        if not self.bridge_log.is_file():
            return BridgeProof(False, "bridge log is missing; priority_udp_bridge is not proven active", endpoint, str(self.bridge_log))
        try:
            for line in self.bridge_log.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") == "bind" and record.get("endpoint") == endpoint:
                    port_ok = int(record.get("bind_port", -1)) == self.bridge_port
                    if port_ok:
                        return BridgeProof(True, "priority_udp_bridge bind event found", endpoint, str(self.bridge_log))
            return BridgeProof(False, "matching priority_udp_bridge bind event was not found", endpoint, str(self.bridge_log))
        except OSError as exc:
            return BridgeProof(False, f"could not read bridge log: {exc}", endpoint, str(self.bridge_log))

    async def wait_for_bridge_bind(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline:
            proof = self.check_bridge_bind()
            self.stats["last_bridge_proof"] = asdict(proof)
            if proof.ok:
                self.emit("modeled_bridge_ready", **asdict(proof))
                return True
            await asyncio.sleep(0.25)
        proof = self.check_bridge_bind()
        self.stats["last_bridge_proof"] = asdict(proof)
        self.emit("modeled_bridge_unavailable", **asdict(proof))
        return False

    def modeled_path_ready(self) -> tuple[bool, str, TraceStatus, BridgeProof]:
        bridge_proof = self.check_bridge_bind()
        trace = read_latest_trace(self.ns3_trace, self.max_trace_age_s, self.link_down_snr_db)
        self.stats["last_trace"] = asdict(trace)
        self.stats["last_bridge_proof"] = asdict(bridge_proof)
        if not self.require_modeled_path:
            return True, "modeled path gate disabled for non-P0 convenience", trace, bridge_proof
        if not bridge_proof.ok:
            return False, bridge_proof.reason, trace, bridge_proof
        if not trace.ok:
            return False, trace.reason, trace, bridge_proof
        return True, "fresh modeled bridge/ns-3 evidence is present", trace, bridge_proof

    def mirror_packet(self, data: bytes, direction: str) -> None:
        payload = json.dumps(
            {
                "type": "manual_gcs_mirror",
                "direction": direction,
                "uav": self.uav["name"],
                "traffic_class": self.traffic_class,
                "time_s": time.time(),
                "payload_bytes": len(data),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii") + b"\n" + data
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, (self.bridge_host, self.bridge_port))
        self.stats["mirrored_packets"] += 1

    async def pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                ready, reason, trace, bridge_proof = self.modeled_path_ready()
                self.mirror_packet(data, direction)
                if not ready:
                    self.stats["dropped_packets"] += 1
                    self.stats["last_drop_reason"] = reason
                    self.emit(
                        "drop",
                        direction=direction,
                        bytes=len(data),
                        reason=reason,
                        trace=asdict(trace),
                        bridge_bind=asdict(bridge_proof),
                    )
                    continue
                writer.write(data)
                await writer.drain()
                if direction == "gcs_to_sitl":
                    self.stats["gcs_to_sitl_packets"] += 1
                    self.stats["bytes_gcs_to_sitl"] += len(data)
                else:
                    self.stats["sitl_to_gcs_packets"] += 1
                    self.stats["bytes_sitl_to_gcs"] += len(data)
                self.emit(
                    "forward",
                    direction=direction,
                    bytes=len(data),
                    trace=asdict(trace),
                    bridge_bind=asdict(bridge_proof),
                )
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception as exc:  # pragma: no cover - defensive runtime logging.
            self.emit("pipe_error", direction=direction, error=str(exc))

    async def handle_client(self, gcs_reader: asyncio.StreamReader, gcs_writer: asyncio.StreamWriter) -> None:
        peer = gcs_writer.get_extra_info("peername")
        self.stats["connections"] += 1
        self.emit(
            "gcs_connect",
            peer=str(peer),
            internal_sitl_tail=f"tcp:{self.sitl_host}:{self.sitl_port}",
            direct_sitl_operator_endpoint=False,
            direct_sitl_p0_eligible=False,
        )
        try:
            sitl_reader, sitl_writer = await asyncio.open_connection(self.sitl_host, self.sitl_port)
        except OSError as exc:
            self.emit("sitl_connect_failed", peer=str(peer), error=str(exc))
            gcs_writer.close()
            await gcs_writer.wait_closed()
            return

        tasks = [
            asyncio.create_task(self.pipe(gcs_reader, sitl_writer, "gcs_to_sitl")),
            asyncio.create_task(self.pipe(sitl_reader, gcs_writer, "sitl_to_gcs")),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                task.result()
            except Exception as exc:
                self.emit("client_task_error", peer=str(peer), error=str(exc))
        sitl_writer.close()
        gcs_writer.close()
        await asyncio.gather(sitl_writer.wait_closed(), gcs_writer.wait_closed(), return_exceptions=True)
        self.emit("gcs_disconnect", peer=str(peer))

    async def run(self) -> int:
        self.stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:  # pragma: no cover - Windows fallback.
                pass

        if self.require_modeled_path and not await self.wait_for_bridge_bind():
            self.write_summary()
            return 3

        server = await asyncio.start_server(self.handle_client, self.bind_host, self.bind_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
        self.emit(
            "listen",
            sockets=sockets,
            p0_guard="direct SITL ports are internal only; operator endpoint is the modeled bridge endpoint",
        )
        self.write_summary()
        async with server:
            await self.stop_event.wait()
            server.close()
            await server.wait_closed()
        self.write_summary()
        return 0


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required: python3 -m pip install PyYAML")
    return yaml.safe_load(path.read_text()) or {}


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def read_latest_trace(path: Path, max_age_s: float, link_down_snr_db: float) -> TraceStatus:
    if not path.is_file():
        return TraceStatus(False, "ns-3/Sionna trace is missing", str(path))
    try:
        mtime_age_s = time.time() - path.stat().st_mtime
        if mtime_age_s > max_age_s:
            return TraceStatus(False, "ns-3/Sionna trace is stale", str(path), mtime_age_s=mtime_age_s)
        rows = list(csv.DictReader(path.open("r", encoding="utf-8")))
    except OSError as exc:
        return TraceStatus(False, f"could not read ns-3/Sionna trace: {exc}", str(path))
    if not rows:
        return TraceStatus(False, "ns-3/Sionna trace has no samples", str(path), mtime_age_s=mtime_age_s)
    row = rows[-1]
    snr = parse_float(row.get("snr_db") or row.get("sinr_db"))
    rssi = parse_float(row.get("rssi_dbm") or row.get("rx_power_dbm"))
    tx_stale = parse_bool(row.get("tx_stale", False))
    rx_stale = parse_bool(row.get("rx_stale", False))
    source = row.get("source")
    status = TraceStatus(
        True,
        "fresh ns-3/Sionna trace sample is usable",
        str(path),
        mtime_age_s=mtime_age_s,
        snr_db=snr,
        rssi_dbm=rssi,
        source=source,
        tx_stale=tx_stale,
        rx_stale=rx_stale,
    )
    if tx_stale or rx_stale:
        status.ok = False
        status.reason = "ns-3/Sionna trace marks tx or rx state stale"
    elif snr is None or not math.isfinite(snr):
        status.ok = False
        status.reason = "ns-3/Sionna trace has no finite SNR/SINR"
    elif snr <= link_down_snr_db:
        status.ok = False
        status.reason = f"modeled link is down or below threshold ({snr:.3f} dB <= {link_down_snr_db:.3f} dB)"
    return status


def manual_default_uav(config: dict[str, Any]) -> str:
    ground = config.get("bridge", {}).get("ground_control", {})
    manual = ground.get("manual_gcs", {})
    return str(manual.get("default_uav") or ground.get("default_uav") or "uav1")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", type=Path, default=DEFAULT_ENDPOINTS)
    parser.add_argument("--run-id", default="manual_gcs_bridge")
    parser.add_argument("--run-dir", type=Path, default=ROOT_DIR / "runs" / "manual_gcs_bridge")
    parser.add_argument("--uav", default=None)
    parser.add_argument("--traffic-class", default=None)
    parser.add_argument("--bind-host", default=None)
    parser.add_argument("--bind-port", type=int, default=None)
    parser.add_argument("--bridge-log", type=Path, default=None)
    parser.add_argument("--ns3-trace", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--max-trace-age-s", type=float, default=5.0)
    parser.add_argument("--link-down-snr-db", type=float, default=-120.0)
    parser.add_argument("--startup-timeout-s", type=float, default=30.0)
    parser.add_argument("--no-require-modeled-path", dest="require_modeled_path", action="store_false")
    parser.set_defaults(require_modeled_path=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        trace = tmp_path / "trace.csv"
        trace.write_text(
            "time_s,source,tx,rx,snr_db,rssi_dbm,tx_stale,rx_stale\n"
            "0.0,ros_odometry,uav1,uav2,42.0,-78.0,false,false\n",
            encoding="utf-8",
        )
        ok = read_latest_trace(trace, 60.0, -120.0)
        if not ok.ok:
            print(f"FAIL expected fresh trace to pass: {ok.reason}", file=sys.stderr)
            return 1
        trace.write_text(
            "time_s,source,tx,rx,snr_db,rssi_dbm,tx_stale,rx_stale\n"
            "0.0,ros_odometry,uav1,uav2,-inf,-inf,false,false\n",
            encoding="utf-8",
        )
        down = read_latest_trace(trace, 60.0, -120.0)
        if down.ok:
            print("FAIL expected -inf trace to be gated closed", file=sys.stderr)
            return 1

    config = load_yaml(DEFAULT_ENDPOINTS)
    args = parse_args(["--run-dir", str(ROOT_DIR / "runs" / "manual_gcs_self_test")])
    args.uav = manual_default_uav(config)
    bridge = ManualGcsBridge(args, config)
    record = bridge.dry_run_record()
    if record["direct_sitl_p0_eligible"]:
        print("FAIL direct SITL must never be P0 eligible", file=sys.stderr)
        return 1
    if not str(record["gcs_endpoint"]).startswith("tcp:127.0.0.1:"):
        print(f"FAIL unexpected GCS endpoint: {record['gcs_endpoint']}", file=sys.stderr)
        return 1
    print("PASS manual_gcs_bridge self-test")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_yaml(args.endpoints)
        if args.uav is None:
            args.uav = manual_default_uav(config)
        bridge = ManualGcsBridge(args, config)
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2

    if args.self_test:
        return run_self_test()
    if args.dry_run:
        print(json.dumps(bridge.dry_run_record(), indent=2, sort_keys=True))
        return 0

    try:
        return asyncio.run(bridge.run())
    except KeyboardInterrupt:
        bridge.write_summary()
        return 130
    except Exception as exc:
        bridge.emit("fatal", error=str(exc))
        bridge.write_summary()
        print(f"FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
