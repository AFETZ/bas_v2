#!/usr/bin/env python3
"""TCP JSON-lines Sionna RT radio provider.

The default mode requires Sionna RT and attempts a real path calculation.
The explicit ``test_free_space`` mode exists only for unit and dependency
smoke tests and is not customer-acceptance eligible.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised in dependency check
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO = ROOT_DIR / "network/config/scenario_5uav.yaml"
DEFAULT_RADIO = ROOT_DIR / "network/config/radio_24ghz.yaml"
DEFAULT_JAMMERS = ROOT_DIR / "network/config/jammers.yaml"
DEFAULT_SERVICE_TIERS = ROOT_DIR / "network/config/service_tiers.yaml"
FLOOR_W = 1e-30
FLOOR_GAIN = 1e-30


class ProviderError(RuntimeError):
    """Actionable provider error returned to callers."""


@dataclass(frozen=True)
class RuntimeFiles:
    scenario: Path
    radio: Path
    jammers: Path
    service_tiers: Path


@dataclass(frozen=True)
class ProviderSettings:
    mode: str
    scene_id: str
    radio: dict[str, Any]
    sionna: dict[str, Any]
    service_selection: list[dict[str, Any]]

    @property
    def carrier_hz(self) -> float:
        return float(self.radio.get("carrier_hz", 2.4e9))

    @property
    def bandwidth_hz(self) -> float:
        return float(self.radio.get("bandwidth_hz", 1e6))

    @property
    def tx_power_dbm(self) -> float:
        return float(self.radio.get("tx_power_dbm", 33.0))

    @property
    def noise_figure_db(self) -> float:
        return float(self.radio.get("receiver_noise_figure_db", 6.0))

    @property
    def sensitivity_dbm(self) -> float:
        return float(self.radio.get("receiver_sensitivity_dbm", -105.0))


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ProviderError(f"YAML file must contain a mapping: {path}")
    return data


def load_runtime_files(args: argparse.Namespace) -> RuntimeFiles:
    return RuntimeFiles(
        scenario=Path(args.scenario).resolve(),
        radio=Path(args.radio_config).resolve(),
        jammers=Path(args.jammers_config).resolve(),
        service_tiers=Path(args.service_tiers).resolve(),
    )


def load_settings(files: RuntimeFiles, mode: str) -> ProviderSettings:
    radio_cfg = load_yaml(files.radio)
    scene_cfg = radio_cfg.get("sionna", {}).get("scene", {})
    return ProviderSettings(
        mode=mode,
        scene_id=str(scene_cfg.get("id", "engineering_10km_v1")),
        radio=dict(radio_cfg.get("radio", {})),
        sionna=dict(radio_cfg.get("sionna", {})),
        service_selection=list(radio_cfg.get("service_tier_selection", [])),
    )


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_run_dir(path: str | Path | None) -> Path:
    run_dir = Path(path).resolve() if path else ROOT_DIR / "runs" / utc_run_id()
    for subdir in ("logs", "heatmaps", "metrics"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    return run_dir


def compact_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), allow_nan=False)


def dbm_to_watt(dbm: float) -> float:
    return 10 ** ((dbm - 30.0) / 10.0)


def watt_to_dbm(watt: float) -> float:
    return 10.0 * math.log10(max(watt, FLOOR_W)) + 30.0


def path_gain_to_loss_db(gain: float) -> float:
    return -10.0 * math.log10(max(gain, FLOOR_GAIN))


def free_space_pathloss_db(distance_m: float, carrier_hz: float) -> float:
    distance = max(float(distance_m), 1.0)
    return 20.0 * math.log10(distance) + 20.0 * math.log10(carrier_hz) - 147.55


def distance_m(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


def finite_round(value: float, digits: int = 3) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(float(value), digits)


def normalize_position(value: Any, field_name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ProviderError(f"{field_name} must be a three-element list")
    return [float(value[0]), float(value[1]), float(value[2])]


def radio_from_request(settings: ProviderSettings, request: dict[str, Any]) -> dict[str, float]:
    radio = dict(settings.radio)
    radio.update(request.get("radio") or {})
    return {
        "carrier_hz": float(radio.get("carrier_hz", settings.carrier_hz)),
        "bandwidth_hz": float(radio.get("bandwidth_hz", settings.bandwidth_hz)),
        "tx_power_dbm": float(radio.get("tx_power_dbm", settings.tx_power_dbm)),
        "receiver_noise_figure_db": float(
            radio.get("receiver_noise_figure_db", settings.noise_figure_db)
        ),
        "receiver_sensitivity_dbm": float(
            radio.get("receiver_sensitivity_dbm", settings.sensitivity_dbm)
        ),
    }


def noise_floor_dbm(bandwidth_hz: float, noise_figure_db: float) -> float:
    return -174.0 + 10.0 * math.log10(max(bandwidth_hz, 1.0)) + noise_figure_db


def channel_overlap_fraction(
    carrier_hz: float,
    bandwidth_hz: float,
    emitter_center_hz: float,
    emitter_bandwidth_hz: float,
) -> float:
    rx_min = carrier_hz - bandwidth_hz / 2.0
    rx_max = carrier_hz + bandwidth_hz / 2.0
    tx_min = emitter_center_hz - emitter_bandwidth_hz / 2.0
    tx_max = emitter_center_hz + emitter_bandwidth_hz / 2.0
    overlap = max(0.0, min(rx_max, tx_max) - max(rx_min, tx_min))
    return min(1.0, overlap / max(bandwidth_hz, 1.0))


def select_service_tier(
    settings: ProviderSettings, sinr_db: float, rssi_dbm: float, sensitivity_dbm: float
) -> tuple[int, float, str]:
    if rssi_dbm < sensitivity_dbm:
        return 0, 1.0, "down"

    policies = settings.service_selection or [
        {"min_sinr_db": 12.0, "service_tier_bps": 100000, "per_input": 0.02, "link_state": "usable"},
        {"min_sinr_db": -999.0, "service_tier_bps": 0, "per_input": 1.0, "link_state": "down"},
    ]
    for entry in sorted(policies, key=lambda item: float(item.get("min_sinr_db", -999.0)), reverse=True):
        if sinr_db >= float(entry.get("min_sinr_db", -999.0)):
            return (
                int(entry.get("service_tier_bps", 0)),
                float(entry.get("per_input", 1.0)),
                str(entry.get("link_state", "unknown")),
            )
    return 0, 1.0, "down"


def nodes_from_scenario(scenario_data: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    command_post = dict(scenario_data.get("command_post") or {})
    if command_post:
        nodes.append(
            {
                "id": command_post.get("id", "cp"),
                "role": command_post.get("role", "command_post"),
                "position_m": normalize_position(
                    command_post.get("position_m", [0.0, 0.0, 20.0]),
                    "command_post.position_m",
                ),
                "orientation_quat_xyzw": command_post.get(
                    "orientation_quat_xyzw", [0.0, 0.0, 0.0, 1.0]
                ),
                "antenna": command_post.get("antenna", "omni"),
            }
        )

    for robot in scenario_data.get("robots", []):
        position = robot.get("nominal_radio_position_m")
        if position is None:
            launch_position = robot.get("position", [0.0, 0.0, 0.0])
            position = [launch_position[0], launch_position[1], launch_position[2]]
        nodes.append(
            {
                "id": robot["name"],
                "role": robot.get("role", "uav"),
                "position_m": normalize_position(position, f"{robot['name']}.position_m"),
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "antenna": robot.get("antenna", "omni"),
            }
        )
    return nodes


def emitters_from_config(jammers_data: dict[str, Any], enabled_only: bool = True) -> list[dict[str, Any]]:
    emitters: list[dict[str, Any]] = []
    for jammer in jammers_data.get("jammers", []):
        if enabled_only and not bool(jammer.get("enabled", False)):
            continue
        emitters.append(
            {
                "id": jammer["id"],
                "position_m": normalize_position(jammer.get("position_m"), f"{jammer['id']}.position_m"),
                "center_hz": float(jammer.get("center_hz", 2.4e9)),
                "bandwidth_hz": float(jammer.get("bandwidth_hz", 1e6)),
                "power_dbm": float(jammer.get("power_dbm", 40.0)),
                "duty_cycle": float(jammer.get("duty_cycle", 1.0)),
                "antenna": jammer.get("antenna", "omni"),
            }
        )
    return emitters


def build_sample_request(
    files: RuntimeFiles,
    include_jammers: bool,
    all_uavs: bool,
    traffic_class: str,
) -> dict[str, Any]:
    scenario = load_yaml(files.scenario)
    jammers = load_yaml(files.jammers)
    settings = load_settings(files, "real_sionna")
    nodes = nodes_from_scenario(scenario)
    uav_ids = [node["id"] for node in nodes if node.get("role") == "uav"]
    selected_uavs = uav_ids if all_uavs else uav_ids[:1]
    return {
        "type": "link_query",
        "time_s": time.time(),
        "deadline_ms": 1000,
        "radio": {
            "carrier_hz": settings.carrier_hz,
            "bandwidth_hz": settings.bandwidth_hz,
            "tx_power_dbm": settings.tx_power_dbm,
        },
        "nodes": nodes,
        "emitters": emitters_from_config(jammers, enabled_only=True) if include_jammers else [],
        "links": [
            {"tx": "cp", "rx": uav_id, "traffic_class": traffic_class}
            for uav_id in selected_uavs
        ],
    }


class QueryLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, direction: str, message: dict[str, Any]) -> None:
        record = {
            "wall_time": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "message": message,
        }
        line = compact_json(record) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)


class SionnaRadioProvider:
    def __init__(self, settings: ProviderSettings):
        self.settings = settings
        self._scene_lock = threading.Lock()
        self._rt = None
        self._path_solver = None
        self._scene = None
        self._np = None
        if settings.mode == "real_sionna":
            self._init_sionna()
        elif settings.mode != "test_free_space":
            raise ProviderError(f"Unsupported provider mode: {settings.mode}")

    @property
    def acceptance_eligible(self) -> bool:
        return self.settings.mode == "real_sionna"

    def _init_sionna(self) -> None:
        try:
            import numpy as np  # type: ignore
            import mitsuba as mi  # type: ignore

            mitsuba_variant = os.environ.get("SIONNA_MITSUBA_VARIANT", "llvm_ad_mono_polarized")
            if mitsuba_variant and mi.variant() is None:
                if mitsuba_variant not in mi.variants():
                    raise ProviderError(
                        f"Requested Mitsuba variant is unavailable: {mitsuba_variant}; "
                        f"available variants: {', '.join(mi.variants())}"
                    )
                mi.set_variant(mitsuba_variant)
            import sionna.rt as rt  # type: ignore
        except ImportError as exc:
            raise ProviderError(
                "Sionna RT, Mitsuba, and NumPy are required for mode=real_sionna; "
                "install them in an external environment and rerun, or use "
                "mode=test_free_space only for unit tests."
            ) from exc

        scene_cfg = self.settings.sionna.get("scene", {})
        source = str(scene_cfg.get("source", "sionna_builtin"))
        scene_path = scene_cfg.get("path")
        if source == "sionna_builtin":
            builtin_name = str(scene_cfg.get("builtin_scene", "simple_street_canyon"))
            scene_path = Path(rt.__file__).resolve().parent / "scenes" / builtin_name / f"{builtin_name}.xml"
        elif scene_path:
            scene_path = Path(scene_path)
            if not scene_path.is_absolute():
                scene_path = ROOT_DIR / scene_path

        if scene_path and not Path(scene_path).exists():
            raise ProviderError(f"Configured Sionna scene does not exist: {scene_path}")

        try:
            self._scene = rt.load_scene(str(scene_path) if scene_path else None)
            self._scene.frequency = self.settings.carrier_hz
            self._scene.bandwidth = self.settings.bandwidth_hz
            self._scene.tx_array = rt.PlanarArray(
                num_rows=1, num_cols=1, pattern="iso", polarization="V"
            )
            self._scene.rx_array = rt.PlanarArray(
                num_rows=1, num_cols=1, pattern="iso", polarization="V"
            )
            self._path_solver = rt.PathSolver()
            self._rt = rt
            self._np = np
        except Exception as exc:  # pragma: no cover - depends on Sionna runtime
            raise ProviderError(f"Failed to initialize Sionna RT scene: {exc}") from exc

    def query(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if request.get("type") != "link_query":
            raise ProviderError("Request type must be 'link_query'")

        radio = radio_from_request(self.settings, request)
        nodes = request.get("nodes")
        links = request.get("links")
        emitters = request.get("emitters", [])
        if not isinstance(nodes, list) or not nodes:
            raise ProviderError("Request must contain a non-empty 'nodes' list")
        if not isinstance(links, list):
            raise ProviderError("Request must contain a 'links' list")
        if not isinstance(emitters, list):
            raise ProviderError("Request field 'emitters' must be a list")

        node_map = {str(node["id"]): node for node in nodes}
        emitter_map = {str(emitter["id"]): emitter for emitter in emitters}
        gains = self._pair_gains(node_map, emitter_map, links, radio)

        response_links: list[dict[str, Any]] = []
        latency_ms = 0.0
        for link in links:
            tx = str(link.get("tx"))
            rx = str(link.get("rx"))
            traffic_class = str(link.get("traffic_class", "control"))
            if tx not in node_map:
                raise ProviderError(f"Unknown link transmitter: {tx}")
            if rx not in node_map:
                raise ProviderError(f"Unknown link receiver: {rx}")

            signal_gain = max(gains.get((tx, rx), 0.0), 0.0)
            pathloss_db = path_gain_to_loss_db(signal_gain)
            rssi_dbm = radio["tx_power_dbm"] + 10.0 * math.log10(max(signal_gain, FLOOR_GAIN))

            jammer_power_w = self._jammer_power_w(rx, emitter_map, gains, radio)
            signal_w = dbm_to_watt(rssi_dbm)
            noise_w = dbm_to_watt(noise_floor_dbm(radio["bandwidth_hz"], radio["receiver_noise_figure_db"]))
            sinr_db = 10.0 * math.log10(max(signal_w, FLOOR_W) / max(noise_w + jammer_power_w, FLOOR_W))
            js_db = watt_to_dbm(jammer_power_w) - rssi_dbm if jammer_power_w > FLOOR_W else -120.0
            service_bps, per_input, link_state = select_service_tier(
                self.settings, sinr_db, rssi_dbm, radio["receiver_sensitivity_dbm"]
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            deadline_ms = float(request.get("deadline_ms", 0) or 0)
            stale = bool(deadline_ms > 0 and latency_ms > deadline_ms)
            response_links.append(
                {
                    "tx": tx,
                    "rx": rx,
                    "traffic_class": traffic_class,
                    "pathloss_db": finite_round(pathloss_db),
                    "rssi_dbm": finite_round(rssi_dbm),
                    "sinr_db": finite_round(sinr_db),
                    "js_db": finite_round(js_db),
                    "service_tier_bps": service_bps,
                    "per_input": finite_round(per_input, 6),
                    "link_state": link_state,
                    "stale": stale,
                }
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        deadline_ms = float(request.get("deadline_ms", 0) or 0)
        if deadline_ms > 0 and latency_ms > deadline_ms:
            for link in response_links:
                link["stale"] = True

        response = {
            "type": "link_state",
            "time_s": float(request.get("time_s", time.time())),
            "provider_latency_ms": finite_round(latency_ms),
            "scene_id": self.settings.scene_id,
            "links": response_links,
        }
        if not self.acceptance_eligible:
            response["test_only"] = True
            response["acceptance_eligible"] = False
        return response

    def _pair_gains(
        self,
        node_map: dict[str, dict[str, Any]],
        emitter_map: dict[str, dict[str, Any]],
        links: list[dict[str, Any]],
        radio: dict[str, float],
    ) -> dict[tuple[str, str], float]:
        if self.settings.mode == "test_free_space":
            return self._free_space_pair_gains(node_map, emitter_map, links, radio)
        return self._sionna_pair_gains(node_map, emitter_map, links, radio)

    def _free_space_pair_gains(
        self,
        node_map: dict[str, dict[str, Any]],
        emitter_map: dict[str, dict[str, Any]],
        links: list[dict[str, Any]],
        radio: dict[str, float],
    ) -> dict[tuple[str, str], float]:
        pairs = self._required_pairs(links, emitter_map)
        gains: dict[tuple[str, str], float] = {}
        for tx, rx in pairs:
            tx_pos = self._entity_position(tx, node_map, emitter_map)
            rx_pos = self._entity_position(rx, node_map, emitter_map)
            loss_db = free_space_pathloss_db(distance_m(tx_pos, rx_pos), radio["carrier_hz"])
            gains[(tx, rx)] = 10.0 ** (-loss_db / 10.0)
        return gains

    def _sionna_pair_gains(
        self,
        node_map: dict[str, dict[str, Any]],
        emitter_map: dict[str, dict[str, Any]],
        links: list[dict[str, Any]],
        radio: dict[str, float],
    ) -> dict[tuple[str, str], float]:
        assert self._rt is not None
        assert self._scene is not None
        assert self._path_solver is not None
        assert self._np is not None

        pairs = self._required_pairs(links, emitter_map)
        tx_ids = sorted({pair[0] for pair in pairs})
        rx_ids = sorted({pair[1] for pair in pairs})
        with self._scene_lock:
            try:
                for name in list(self._scene.transmitters.keys()) + list(self._scene.receivers.keys()):
                    self._scene.remove(name)
                self._scene.frequency = radio["carrier_hz"]
                self._scene.bandwidth = radio["bandwidth_hz"]
                tx_names = {}
                rx_names = {}
                for tx_id in tx_ids:
                    position = self._entity_position(tx_id, node_map, emitter_map)
                    power_dbm = float(emitter_map[tx_id].get("power_dbm", 40.0)) if tx_id in emitter_map else radio["tx_power_dbm"]
                    device_name = self._device_name("tx", tx_id)
                    tx_names[tx_id] = device_name
                    self._scene.add(self._rt.Transmitter(name=device_name, position=position, power_dbm=power_dbm))
                for rx_id in rx_ids:
                    position = self._entity_position(rx_id, node_map, emitter_map)
                    device_name = self._device_name("rx", rx_id)
                    rx_names[rx_id] = device_name
                    self._scene.add(self._rt.Receiver(name=device_name, position=position))

                solver_cfg = self.settings.sionna.get("solver", {})
                paths = self._path_solver(
                    self._scene,
                    max_depth=int(solver_cfg.get("max_depth", 1)),
                    samples_per_src=int(solver_cfg.get("samples_per_src", 128)),
                    synthetic_array=bool(solver_cfg.get("synthetic_array", True)),
                    los=bool(solver_cfg.get("los", True)),
                    specular_reflection=bool(solver_cfg.get("specular_reflection", True)),
                    diffuse_reflection=bool(solver_cfg.get("diffuse_reflection", False)),
                    refraction=bool(solver_cfg.get("refraction", False)),
                    diffraction=bool(solver_cfg.get("diffraction", False)),
                    seed=int(solver_cfg.get("seed", 42)),
                )
                amplitudes = self._np.asarray(paths.a)
                tx_index = {tx_id: idx for idx, tx_id in enumerate(tx_ids)}
                rx_index = {rx_id: idx for idx, rx_id in enumerate(rx_ids)}
                gains: dict[tuple[str, str], float] = {}
                for tx_id, rx_id in pairs:
                    gain = float(
                        self._np.sum(
                            self._np.abs(amplitudes[:, rx_index[rx_id], :, tx_index[tx_id], :, :]) ** 2
                        )
                    )
                    gains[(tx_id, rx_id)] = max(gain, 0.0)
                return gains
            except Exception as exc:
                raise ProviderError(f"Sionna RT link query failed: {exc}") from exc

    def _required_pairs(
        self, links: list[dict[str, Any]], emitter_map: dict[str, dict[str, Any]]
    ) -> set[tuple[str, str]]:
        pairs = {(str(link.get("tx")), str(link.get("rx"))) for link in links}
        receivers = {str(link.get("rx")) for link in links}
        for emitter_id in emitter_map:
            for rx in receivers:
                pairs.add((emitter_id, rx))
        return pairs

    def _entity_position(
        self,
        entity_id: str,
        node_map: dict[str, dict[str, Any]],
        emitter_map: dict[str, dict[str, Any]],
    ) -> list[float]:
        if entity_id in node_map:
            return normalize_position(node_map[entity_id].get("position_m"), f"{entity_id}.position_m")
        if entity_id in emitter_map:
            return normalize_position(emitter_map[entity_id].get("position_m"), f"{entity_id}.position_m")
        raise ProviderError(f"Unknown radio entity: {entity_id}")

    def _jammer_power_w(
        self,
        rx: str,
        emitter_map: dict[str, dict[str, Any]],
        gains: dict[tuple[str, str], float],
        radio: dict[str, float],
    ) -> float:
        total_w = 0.0
        for emitter_id, emitter in emitter_map.items():
            duty_cycle = max(0.0, min(1.0, float(emitter.get("duty_cycle", 1.0))))
            overlap = channel_overlap_fraction(
                radio["carrier_hz"],
                radio["bandwidth_hz"],
                float(emitter.get("center_hz", radio["carrier_hz"])),
                float(emitter.get("bandwidth_hz", radio["bandwidth_hz"])),
            )
            if duty_cycle <= 0.0 or overlap <= 0.0:
                continue
            gain = max(gains.get((emitter_id, rx), 0.0), 0.0)
            power_dbm = float(emitter.get("power_dbm", 40.0))
            total_w += dbm_to_watt(power_dbm) * gain * duty_cycle * overlap
        return total_w

    def _device_name(self, prefix: str, entity_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in entity_id)
        return f"{prefix}_{safe}"


class JSONLProviderServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        provider: SionnaRadioProvider,
        logger: QueryLogger,
    ):
        super().__init__(server_address, JSONLRequestHandler)
        self.provider = provider
        self.logger = logger


class JSONLRequestHandler(socketserver.StreamRequestHandler):
    server: JSONLProviderServer

    def handle(self) -> None:
        for raw_line in self.rfile:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ProviderError("JSON line must decode to an object")
                self.server.logger.write("request", request)
                response = self.server.provider.query(request)
            except Exception as exc:
                response = {
                    "type": "error",
                    "time_s": time.time(),
                    "error": str(exc),
                }
            self.server.logger.write("response", response)
            self.wfile.write((compact_json(response) + "\n").encode("utf-8"))


def query_tcp(host: str, port: int, request: dict[str, Any], timeout_s: float = 10.0) -> dict[str, Any]:
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.sendall((compact_json(request) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data:
        raise ProviderError("Provider returned no response")
    response = json.loads(data.decode("utf-8"))
    if not isinstance(response, dict):
        raise ProviderError("Provider response was not a JSON object")
    return response


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate_heatmaps(
    provider: SionnaRadioProvider,
    files: RuntimeFiles,
    run_dir: Path,
    grid_points: int | None,
    altitude_m: float | None,
    include_jammers: bool,
) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore

    radio_cfg = load_yaml(files.radio)
    scenario = load_yaml(files.scenario)
    jammers = load_yaml(files.jammers)
    heatmap_cfg = radio_cfg.get("heatmaps", {})
    points = int(grid_points or heatmap_cfg.get("default_grid_points", 31))
    if points < 2:
        raise ProviderError("--grid-points must be at least 2")
    altitude = float(altitude_m if altitude_m is not None else heatmap_cfg.get("altitude_m", 80.0))
    extent = list(heatmap_cfg.get("extent_m", [-5000.0, 5000.0, -5000.0, 5000.0]))
    if len(extent) != 4:
        raise ProviderError("heatmaps.extent_m must contain [xmin, xmax, ymin, ymax]")
    xmin, xmax, ymin, ymax = [float(value) for value in extent]

    nodes = nodes_from_scenario(scenario)
    sample_nodes: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    for yi in range(points):
        y = ymin + (ymax - ymin) * yi / (points - 1)
        for xi in range(points):
            x = xmin + (xmax - xmin) * xi / (points - 1)
            node_id = f"sample_{yi}_{xi}"
            sample_nodes.append(
                {
                    "id": node_id,
                    "role": "heatmap_sample",
                    "position_m": [x, y, altitude],
                    "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "antenna": "omni",
                }
            )
            links.append({"tx": "cp", "rx": node_id, "traffic_class": "control"})

    request = {
        "type": "link_query",
        "time_s": time.time(),
        "deadline_ms": 30000,
        "radio": {
            "carrier_hz": provider.settings.carrier_hz,
            "bandwidth_hz": provider.settings.bandwidth_hz,
            "tx_power_dbm": provider.settings.tx_power_dbm,
        },
        "nodes": nodes[:1] + sample_nodes,
        "emitters": emitters_from_config(jammers, enabled_only=True) if include_jammers else [],
        "links": links,
    }
    response = provider.query(request)

    matrices: dict[str, list[list[float]]] = {
        "rss": [[0.0 for _ in range(points)] for _ in range(points)],
        "sinr": [[0.0 for _ in range(points)] for _ in range(points)],
        "js": [[0.0 for _ in range(points)] for _ in range(points)],
        "degradation_zone": [[0.0 for _ in range(points)] for _ in range(points)],
        "service_tier": [[0.0 for _ in range(points)] for _ in range(points)],
    }
    degradation_sinr = float(heatmap_cfg.get("degradation_sinr_db", 6.0))
    for item in response["links"]:
        _, yi, xi = item["rx"].split("_")
        row = int(yi)
        col = int(xi)
        matrices["rss"][row][col] = float(item["rssi_dbm"])
        matrices["sinr"][row][col] = float(item["sinr_db"])
        matrices["js"][row][col] = float(item["js_db"])
        matrices["degradation_zone"][row][col] = 1.0 if float(item["sinr_db"]) < degradation_sinr else 0.0
        matrices["service_tier"][row][col] = float(item["service_tier_bps"])

    heatmap_dir = run_dir / "heatmaps"
    heatmap_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "type": "radio_heatmaps",
        "scene_id": response["scene_id"],
        "provider_mode": provider.settings.mode,
        "test_only": not provider.acceptance_eligible,
        "grid_points": points,
        "altitude_m": altitude,
        "extent_m": [xmin, xmax, ymin, ymax],
        "include_jammers": include_jammers,
        "provider_latency_ms": response["provider_latency_ms"],
    }

    labels = {
        "rss": "RSS (dBm)",
        "sinr": "SINR (dB)",
        "js": "J/S (dB)",
        "degradation_zone": f"Degradation zone (SINR < {degradation_sinr:g} dB)",
        "service_tier": "Service tier (bps)",
    }
    for name, matrix in matrices.items():
        fig, ax = plt.subplots(figsize=(7, 5))
        image = ax.imshow(matrix, extent=[xmin, xmax, ymin, ymax], origin="lower", aspect="auto")
        ax.set_xlabel("x (m)")
        ax.set_ylabel("y (m)")
        ax.set_title(labels[name])
        fig.colorbar(image, ax=ax)
        fig.tight_layout()
        fig.savefig(heatmap_dir / f"{name}.png", dpi=140)
        plt.close(fig)

    write_json(heatmap_dir / "heatmap_summary.json", {"metadata": metadata, "response": response})
    return metadata


def cmd_serve(args: argparse.Namespace) -> int:
    files = load_runtime_files(args)
    run_dir = ensure_run_dir(args.run_dir)
    settings = load_settings(files, args.mode)
    provider = SionnaRadioProvider(settings)
    log_path = run_dir / "logs/sionna_link_queries.jsonl"
    logger = QueryLogger(log_path)
    host = args.host or str(load_yaml(files.radio).get("ipc", {}).get("host", "127.0.0.1"))
    port = int(args.port or load_yaml(files.radio).get("ipc", {}).get("port", 5090))
    server = JSONLProviderServer((host, port), provider, logger)
    actual_host, actual_port = server.server_address
    print(
        f"Sionna radio provider listening on {actual_host}:{actual_port} "
        f"mode={args.mode} scene_id={settings.scene_id} log={log_path}",
        flush=True,
    )
    if not provider.acceptance_eligible:
        print("WARNING mode=test_free_space is test-only and cannot satisfy customer acceptance.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def cmd_oneshot(args: argparse.Namespace) -> int:
    files = load_runtime_files(args)
    settings = load_settings(files, args.mode)
    provider = SionnaRadioProvider(settings)
    if args.request_file == "-":
        request = json.load(sys.stdin)
    else:
        request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
    response = provider.query(request)
    if args.log:
        logger = QueryLogger(Path(args.log))
        logger.write("request", request)
        logger.write("response", response)
    print(compact_json(response))
    return 0


def cmd_sample_request(args: argparse.Namespace) -> int:
    files = load_runtime_files(args)
    request = build_sample_request(files, args.include_jammers, args.all_uavs, args.traffic_class)
    print(json.dumps(request, indent=2, sort_keys=True))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    files = load_runtime_files(args)
    if args.request_file:
        request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
    else:
        request = build_sample_request(files, args.include_jammers, args.all_uavs, args.traffic_class)
    response = query_tcp(args.host, int(args.port), request, timeout_s=args.timeout_s)
    print(compact_json(response))
    return 0


def cmd_heatmap(args: argparse.Namespace) -> int:
    files = load_runtime_files(args)
    run_dir = ensure_run_dir(args.run_dir)
    settings = load_settings(files, args.mode)
    provider = SionnaRadioProvider(settings)
    metadata = generate_heatmaps(
        provider,
        files,
        run_dir,
        grid_points=args.grid_points,
        altitude_m=args.altitude_m,
        include_jammers=args.include_jammers,
    )
    print(compact_json(metadata))
    return 0


def add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO), help="Scenario YAML path.")
    parser.add_argument("--radio-config", default=str(DEFAULT_RADIO), help="2.4 GHz radio YAML path.")
    parser.add_argument("--jammers-config", default=str(DEFAULT_JAMMERS), help="Jammer YAML path.")
    parser.add_argument("--service-tiers", default=str(DEFAULT_SERVICE_TIERS), help="Service tier YAML path.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the TCP JSON-lines provider service.")
    add_common_config_args(serve)
    serve.add_argument("--mode", choices=["real_sionna", "test_free_space"], default="real_sionna")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--run-dir", default=None)
    serve.set_defaults(func=cmd_serve)

    oneshot = subparsers.add_parser("oneshot", help="Evaluate one link_query JSON request locally.")
    add_common_config_args(oneshot)
    oneshot.add_argument("--mode", choices=["real_sionna", "test_free_space"], default="real_sionna")
    oneshot.add_argument("--request-file", required=True, help="Path to request JSON, or '-' for stdin.")
    oneshot.add_argument("--log", default=None, help="Optional query JSONL log path.")
    oneshot.set_defaults(func=cmd_oneshot)

    sample = subparsers.add_parser("sample-request", help="Print a scenario-derived link_query request.")
    add_common_config_args(sample)
    sample.add_argument("--include-jammers", action="store_true")
    sample.add_argument("--all-uavs", action="store_true")
    sample.add_argument("--traffic-class", default="control")
    sample.set_defaults(func=cmd_sample_request)

    query = subparsers.add_parser("query", help="Send a link_query request to a running provider.")
    add_common_config_args(query)
    query.add_argument("--host", default="127.0.0.1")
    query.add_argument("--port", type=int, default=5090)
    query.add_argument("--timeout-s", type=float, default=10.0)
    query.add_argument("--request-file", default=None)
    query.add_argument("--include-jammers", action="store_true")
    query.add_argument("--all-uavs", action="store_true")
    query.add_argument("--traffic-class", default="control")
    query.set_defaults(func=cmd_query)

    heatmap = subparsers.add_parser("heatmap", help="Generate RSS/SINR/J/S/service-tier heatmaps.")
    add_common_config_args(heatmap)
    heatmap.add_argument("--mode", choices=["real_sionna", "test_free_space"], default="real_sionna")
    heatmap.add_argument("--run-dir", default=None)
    heatmap.add_argument("--grid-points", type=int, default=None)
    heatmap.add_argument("--altitude-m", type=float, default=None)
    heatmap.add_argument("--include-jammers", action="store_true")
    heatmap.set_defaults(func=cmd_heatmap)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ProviderError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
