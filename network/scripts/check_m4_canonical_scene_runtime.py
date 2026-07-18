#!/usr/bin/env python3
"""Load the canonical M4 scene in real Sionna RT and exercise its fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from network.radio_provider.provider import (
    RuntimeFiles,
    SionnaRadioProvider,
    load_settings,
)
from network.validation.validate_m4_scene_bundle import (
    DEFAULT_BUNDLE,
    ROOT,
    strict_json,
    validate_scene_bundle,
)


EXPECTED_BUNDLE_SHA256 = "049d9f648eb82165a86f0b5758f984b38c03c5b0d979aaec18d18d361d9da245"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def link_query(
    provider: SionnaRadioProvider,
    *,
    query_id: str,
    tx_position: list[float],
    rx_position: list[float],
    emitters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = {
        "type": "link_query",
        "time_s": time.time(),
        "deadline_ms": 30000,
        "radio": {
            "carrier_hz": 2400000000,
            "bandwidth_hz": 20000000,
            "tx_power_dbm": 33.0,
        },
        "nodes": [
            {"id": "fixture_tx", "role": "command_post", "position_m": tx_position, "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0], "antenna": "omni"},
            {"id": "fixture_rx", "role": "uav", "position_m": rx_position, "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0], "antenna": "omni"},
        ],
        "emitters": list(emitters or []),
        "links": [{"tx": "fixture_tx", "rx": "fixture_rx", "traffic_class": "control"}],
        "fixture_query_id": query_id,
    }
    started_ns = time.monotonic_ns()
    response = provider.query(request)
    completed_ns = time.monotonic_ns()
    return {
        "completed_monotonic_ns": completed_ns,
        "elapsed_ms": (completed_ns - started_ns) / 1_000_000.0,
        "query_id": query_id,
        "request": request,
        "request_sha256": hashlib.sha256(
            (json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
        "response": response,
        "response_sha256": hashlib.sha256(
            (json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
        "started_monotonic_ns": started_ns,
    }


def validate_response(record: dict[str, Any], *, expected_positive: bool, bundle_id: str) -> list[str]:
    failures: list[str] = []
    response = record.get("response")
    if not isinstance(response, dict):
        return [f"{record.get('query_id')}: response is not an object"]
    if response.get("type") != "link_state" or response.get("scene_id") != bundle_id:
        failures.append(f"{record['query_id']}: response type/scene identity mismatch")
    if response.get("test_only") is not None or response.get("acceptance_eligible") is not None:
        failures.append(f"{record['query_id']}: test-only marker is forbidden")
    links = response.get("links")
    if not isinstance(links, list) or len(links) != 1 or not isinstance(links[0], dict):
        return failures + [f"{record['query_id']}: response does not contain exactly one link"]
    link = links[0]
    for field in ("pathloss_db", "rssi_dbm", "sinr_db", "js_db", "per_input"):
        value = link.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            failures.append(f"{record['query_id']}: {field} is not finite")
    if link.get("stale") is not False:
        failures.append(f"{record['query_id']}: 30s runtime-smoke deadline was missed")
    if expected_positive:
        if not isinstance(link.get("service_tier_bps"), int) or link["service_tier_bps"] <= 0 or link.get("link_state") == "down" or float(link.get("per_input", 1.0)) >= 1.0:
            failures.append(f"{record['query_id']}: declared clear fixture is unavailable")
    elif link.get("service_tier_bps") != 0 or link.get("link_state") != "down" or link.get("per_input") != 1.0:
        failures.append(f"{record['query_id']}: declared obstructed fixture is not fail-closed")
    return failures


def run(bundle_path: Path) -> dict[str, Any]:
    static_result = validate_scene_bundle(bundle_path, ROOT)
    failures: list[str] = []
    if static_result.get("status") != "PASS":
        failures.append("independent static scene validation did not pass")
    bundle = strict_json(bundle_path)
    if bundle.get("bundle_sha256") != EXPECTED_BUNDLE_SHA256:
        failures.append("canonical bundle SHA-256 differs from frozen runtime checker")

    files = RuntimeFiles(
        scenario=ROOT / "network/config/scenario_m4_canonical.yaml",
        radio=ROOT / "network/config/radio_m4_canonical.yaml",
        jammers=ROOT / "network/config/jammers_m4_canonical.yaml",
        service_tiers=ROOT / "network/config/service_tiers.yaml",
    )
    settings = load_settings(files, "real_sionna")
    if settings.mode != "real_sionna" or settings.scene_id != bundle.get("bundle_id"):
        failures.append("provider settings are not real_sionna bound to the canonical bundle")
    provider = SionnaRadioProvider(settings)

    import drjit as dr  # type: ignore
    import mitsuba as mi  # type: ignore
    import sionna.rt as rt  # type: ignore

    variant = mi.variant()
    if variant != "cuda_ad_mono_polarized":
        failures.append(f"Mitsuba variant is {variant!r}, expected cuda_ad_mono_polarized")
    cuda_probe = mi.Float([1.25, 2.5, 3.75])
    cuda_sum = dr.sum(cuda_probe * cuda_probe)
    dr.eval(cuda_sum)
    cuda_sum_value = float(cuda_sum[0])
    if not math.isclose(cuda_sum_value, 21.875, rel_tol=0.0, abs_tol=1e-6):
        failures.append("finite Mitsuba/Dr.Jit CUDA computation mismatch")

    records: list[dict[str, Any]] = []
    for fixture in bundle.get("range_fixtures", []):
        geometry = fixture.get("geometry")
        record = link_query(
            provider,
            query_id=str(fixture.get("id")),
            tx_position=list(fixture.get("tx_position_m", [])),
            rx_position=list(fixture.get("rx_position_m", [])),
        )
        records.append(record)
        failures.extend(
            validate_response(
                record,
                expected_positive=geometry == "los",
                bundle_id=str(bundle.get("bundle_id")),
            )
        )

    jammer_scenario = bundle["causal_scenarios"]["jammer_off_on_off"]
    jammer_poses = jammer_scenario["pose_set"]
    off = link_query(
        provider,
        query_id="jammer_off",
        tx_position=list(jammer_poses["cp"]),
        rx_position=list(jammer_poses["uav3"]),
    )
    fixture = bundle["jammer_fixture"]
    on = link_query(
        provider,
        query_id="jammer_on",
        tx_position=list(jammer_poses["cp"]),
        rx_position=list(jammer_poses["uav3"]),
        emitters=[
            {
                "id": fixture["id"],
                "position_m": fixture["position_m"],
                "center_hz": fixture["center_hz"],
                "bandwidth_hz": fixture["bandwidth_hz"],
                "power_dbm": fixture["power_dbm"],
                "duty_cycle": fixture["duty_cycle"],
                "antenna": "omni",
            }
        ],
    )
    records.extend((off, on))
    failures.extend(validate_response(off, expected_positive=True, bundle_id=bundle["bundle_id"]))
    failures.extend(validate_response(on, expected_positive=False, bundle_id=bundle["bundle_id"]))
    off_link = off["response"]["links"][0]
    on_link = on["response"]["links"][0]
    if float(on_link["sinr_db"]) > float(off_link["sinr_db"]) - 3.0:
        failures.append("jammer-on SINR does not degrade by at least 3dB")
    if float(on_link["js_db"]) <= float(off_link["js_db"]):
        failures.append("jammer-on J/S does not increase")

    provider_path = ROOT / "network/radio_provider/provider.py"
    result = {
        "bundle_id": bundle.get("bundle_id"),
        "bundle_sha256": bundle.get("bundle_sha256"),
        "contract": "ams.m4.scene-runtime-smoke/v1",
        "cuda": {"finite_sum": cuda_sum_value, "mitsuba_variant": variant},
        "failures": failures,
        "provider_identity": {
            "mode": settings.mode,
            "provider_path": str(provider_path.relative_to(ROOT)),
            "provider_sha256": file_sha256(provider_path),
            "sionna_rt_path": str(Path(rt.__file__).resolve()),
        },
        "queries": records,
        "schema_version": 1,
        "static_scene_validation": static_result,
        "status": "PASS" if not failures else "FAIL",
    }
    return result


def write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = run(args.bundle.resolve())
    payload = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    if args.json_output:
        write_new(args.json_output, payload)
    else:
        print(payload.decode(), end="")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
