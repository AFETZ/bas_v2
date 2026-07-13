#!/usr/bin/env python3
"""Smoke-test the Sionna provider JSON schema and test-only fallback behavior."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.radio_provider.provider import (
    JSONLProviderServer,
    QueryLogger,
    SionnaRadioProvider,
    build_sample_request,
    load_runtime_files,
    load_settings,
    query_tcp,
)


class Args:
    scenario = str(ROOT_DIR / "network/config/scenario_5uav.yaml")
    radio_config = str(ROOT_DIR / "network/config/radio_24ghz.yaml")
    jammers_config = str(ROOT_DIR / "network/config/jammers.yaml")
    service_tiers = str(ROOT_DIR / "network/config/service_tiers.yaml")


def assert_link_schema(response: dict) -> None:
    assert response["type"] == "link_state"
    assert isinstance(response["provider_latency_ms"], (int, float))
    assert isinstance(response["scene_id"], str)
    assert response["links"], "response must contain at least one link"
    required = {
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
    }
    assert required.issubset(response["links"][0]), response["links"][0]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    files = load_runtime_files(Args())
    settings = load_settings(files, "test_free_space")
    provider = SionnaRadioProvider(settings)
    request = build_sample_request(files, include_jammers=False, all_uavs=False, traffic_class="control")
    baseline = provider.query(request)
    assert baseline["test_only"] is True
    assert baseline["acceptance_eligible"] is False
    assert_link_schema(baseline)

    moved = json.loads(json.dumps(request))
    for node in moved["nodes"]:
        if node["id"] == "uav1":
            node["position_m"][0] += 1000.0
    moved_response = provider.query(moved)
    assert moved_response["links"][0]["pathloss_db"] != baseline["links"][0]["pathloss_db"]

    jammed = json.loads(json.dumps(request))
    jammed["emitters"] = [
        {
            "id": "test_jammer",
            "position_m": [100.0, 80.0, 80.0],
            "center_hz": 2400000000,
            "bandwidth_hz": 1000000,
            "power_dbm": 50.0,
            "duty_cycle": 1.0,
        }
    ]
    jammed_response = provider.query(jammed)
    assert jammed_response["links"][0]["sinr_db"] < baseline["links"][0]["sinr_db"]
    assert jammed_response["links"][0]["js_db"] > baseline["links"][0]["js_db"]

    with tempfile.TemporaryDirectory() as temp_dir:
        logger = QueryLogger(Path(temp_dir) / "sionna_link_queries.jsonl")
        server = JSONLProviderServer(("127.0.0.1", free_port()), provider, logger)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tcp_response = query_tcp("127.0.0.1", int(server.server_address[1]), request)
            assert_link_schema(tcp_response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)
        log_lines = (Path(temp_dir) / "sionna_link_queries.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(log_lines) == 2
        assert json.loads(log_lines[0])["direction"] == "request"
        assert json.loads(log_lines[1])["direction"] == "response"

    print("PASS Sionna provider contract smoke test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
