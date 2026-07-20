#!/usr/bin/env python3
"""Validate selectable ns-3 packet-core fidelity modes.

The current runtime remains the CSMA surrogate. Higher-fidelity external
packet-in-the-loop modes are named here so operators can evaluate prerequisites
without accidentally running a partial, bypass-prone implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]


MODE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "csma_surrogate": {
        "aliases": {"csma", "csma_surrogate", "p2mp_csma", "shared_csma"},
        "status": "implemented_current_p0_surrogate",
        "runtime_selectable": True,
        "shared_medium_model": "csma",
        "fidelity_note_id": "csma_surrogate_not_customer_modem_waveform",
        "upstream_interfaces": ["ns3::CsmaNetDevice", "ns3::FlowMonitor"],
        "limitations": [
            "Uses ns-3 CSMA as a shared-medium packet/MAC surrogate.",
            "Does not model a customer modem waveform, PHY framing, or modem firmware queues.",
            "The legacy generated-flow runner has no external ingress; M2 composes this medium with tap_bridge_external.",
        ],
        "next_required": [
            "Keep using this mode for the current P0 packet-core run.",
            "Use tap_bridge_external evaluation before replacing the bridge-facing packet path.",
        ],
    },
    "tap_bridge_external": {
        "aliases": {
            "tap_bridge_external",
            "tap_bridge_fdnet",
            "tap_bridge",
            "tapbridge",
        },
        "status": "implemented_m2_diagnostic_fail_closed",
        "runtime_selectable": False,
        "shared_medium_model": "csma_via_external_tap_bridge",
        "fidelity_note_id": "tapbridge_diagnostic_not_accepted_m2_yet",
        "upstream_interfaces": ["ns3::TapBridge"],
        "limitations": [
            "The dedicated M2 path connects namespace endpoints and has passed ICMP/opaque-UDP diagnostics.",
            "General P0 runtime selection stays fail-closed until sealed real-MAVLink good/down/recovery evidence passes.",
        ],
        "next_required": [
            "Run the real-MAVLink M2 good/down/recovery transaction suite.",
            "Correlate decoded frame hashes across all external and ns-3 capture points.",
            "Keep the general P0 selector disabled until M0/M1 prerequisites and M2 validation pass.",
        ],
    },
    "customer_modem_model": {
        "aliases": {
            "customer_modem_model",
            "custom_modem_model",
            "modem_model",
            "real_modem_packet_model",
        },
        "status": "future_fail_closed",
        "runtime_selectable": False,
        "shared_medium_model": "customer_modem_specific",
        "fidelity_note_id": "customer_modem_model_not_supplied",
        "upstream_interfaces": [],
        "limitations": [
            "No customer modem waveform, queue, firmware timing, or hardware adapter contract is present.",
            "Physical modem hardware must not be probed by the current simulated P0 runtime.",
        ],
        "next_required": [
            "Receive customer modem interface/timing documentation.",
            "Define a no-hardware evaluation harness before any live RF or device probing.",
        ],
    },
}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"config file must contain a YAML mapping: {path}")
    return data


def normalize_mode(value: str | None) -> str:
    if not value:
        raise ValueError("packet-core mode is empty")
    normalized = value.strip().lower().replace("-", "_")
    for mode, definition in MODE_DEFINITIONS.items():
        aliases = {alias.replace("-", "_") for alias in definition["aliases"]}
        if normalized == mode or normalized in aliases:
            return mode
    allowed = ", ".join(sorted(MODE_DEFINITIONS))
    raise ValueError(f"unknown packet-core mode {value!r}; allowed modes: {allowed}")


def resolve_mode(radio_config: dict[str, Any], requested_mode: str | None = None) -> str:
    if requested_mode:
        return normalize_mode(requested_mode)

    ns3 = radio_config.get("ns3", {})
    if not isinstance(ns3, dict):
        raise ValueError("radio config field 'ns3' must be a mapping")

    explicit_mode = ns3.get("packet_core_mode")
    if explicit_mode:
        return normalize_mode(str(explicit_mode))

    shared_medium = str(ns3.get("shared_medium_model", "csma"))
    return normalize_mode(shared_medium)


def mode_definition(mode: str) -> dict[str, Any]:
    return MODE_DEFINITIONS[normalize_mode(mode)]


def _check(name: str, ok: bool, detail: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": bool(required), "detail": detail}


def _check_command(command: str, required: bool = True) -> dict[str, Any]:
    path = shutil.which(command)
    return _check(
        f"command:{command}",
        path is not None,
        path or f"{command} not found on PATH",
        required,
    )


def _check_ns3_launcher(ns3_dir: Path, required: bool = True) -> dict[str, Any]:
    candidates = [ns3_dir / "ns3", ns3_dir / "waf"]
    found = [str(path) for path in candidates if path.exists() and os.access(path, os.X_OK)]
    return _check(
        "ns3:launcher",
        bool(found),
        found[0] if found else f"expected executable ns3 or waf under {ns3_dir}",
        required,
    )


def _check_ns3_module(ns3_dir: Path, module: str, required: bool = True) -> dict[str, Any]:
    module_dir = ns3_dir / "src" / module
    return _check(
        f"ns3:module:{module}",
        module_dir.is_dir(),
        str(module_dir) if module_dir.is_dir() else f"missing {module_dir}",
        required,
    )


def _check_ip_netns(required: bool = True) -> dict[str, Any]:
    ip = shutil.which("ip")
    if not ip:
        return _check("host:ip_netns", False, "ip command not found", required)
    result = subprocess.run([ip, "netns", "list"], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return _check("host:ip_netns", True, "ip netns list succeeded", required)
    detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    return _check("host:ip_netns", False, detail, required)


def _radio_backend_checks(radio_backend: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    default_backend = radio_backend.get("default_backend")
    checks.append(
        _check(
            "config:default_backend",
            default_backend == "sim_2_4ghz",
            f"default_backend={default_backend!r}",
        )
    )

    sim_backend = radio_backend.get("backends", {}).get("sim_2_4ghz", {})
    packet_modes = sim_backend.get("packet_core_modes", {})
    checks.append(
        _check(
            "config:packet_core_modes",
            isinstance(packet_modes, dict) and mode in packet_modes,
            f"configured modes={sorted(packet_modes) if isinstance(packet_modes, dict) else '<missing>'}",
        )
    )

    return checks


def _endpoint_checks(endpoints: dict[str, Any]) -> list[dict[str, Any]]:
    isolation = endpoints.get("isolation", {})
    bridge = endpoints.get("bridge", {})
    return [
        _check(
            "config:isolation_model",
            bool(isolation.get("model")),
            f"model={isolation.get('model')!r}",
        ),
        _check(
            "config:bridge_ns3_handoff",
            isinstance(bridge.get("ns3"), dict),
            "bridge.ns3 present" if isinstance(bridge.get("ns3"), dict) else "bridge.ns3 missing",
        ),
    ]


def build_mode_report(
    *,
    radio_config: dict[str, Any],
    radio_backend: dict[str, Any],
    endpoints: dict[str, Any],
    ns3_dir: Path,
    requested_mode: str | None = None,
    purpose: str = "evaluation",
    skip_host_checks: bool = False,
) -> dict[str, Any]:
    mode = resolve_mode(radio_config, requested_mode)
    definition = mode_definition(mode)
    checks: list[dict[str, Any]] = []
    checks.extend(_radio_backend_checks(radio_backend, mode))
    checks.extend(_endpoint_checks(endpoints))

    if mode == "csma_surrogate":
        checks.append(_check_ns3_module(ns3_dir, "csma", required=purpose == "runtime"))
        checks.append(
            _check(
                "source:ams_radio_core",
                (ROOT / "network/ns3/scratch/ams-radio-core.cc").is_file(),
                "network/ns3/scratch/ams-radio-core.cc",
            )
        )
    elif mode == "tap_bridge_external":
        checks.append(_check_ns3_launcher(ns3_dir, required=False))
        checks.append(_check_ns3_module(ns3_dir, "tap-bridge", required=False))
        checks.append(_check_command("ip", required=False))
        if skip_host_checks:
            checks.append(_check("host:tun_device", True, "skipped by --skip-host-checks", required=False))
            checks.append(_check("host:ip_netns", True, "skipped by --skip-host-checks", required=False))
        else:
            checks.append(
                _check(
                    "host:tun_device",
                    Path("/dev/net/tun").exists(),
                    "/dev/net/tun present" if Path("/dev/net/tun").exists() else "/dev/net/tun missing",
                    required=False,
                )
            )
            checks.append(_check_ip_netns(required=False))
    elif mode == "customer_modem_model":
        checks.append(
            _check(
                "config:customer_modem_contract",
                False,
                "no customer modem packet model or no-hardware harness is configured",
                required=False,
            )
        )

    dependency_ready = all(check["ok"] for check in checks if check["required"])
    runtime_selectable = bool(definition["runtime_selectable"])
    can_run = runtime_selectable and dependency_ready
    missing_required = [check for check in checks if check["required"] and not check["ok"]]
    missing_optional = [check for check in checks if not check["required"] and not check["ok"]]

    return {
        "schema_version": 1,
        "mode": mode,
        "requested_mode": requested_mode or "radio_config_default",
        "purpose": purpose,
        "status": definition["status"],
        "runtime_selectable": runtime_selectable,
        "fail_closed": not runtime_selectable,
        "can_run": can_run,
        "dependency_ready": dependency_ready,
        "shared_medium_model": definition["shared_medium_model"],
        "fidelity_note_id": definition["fidelity_note_id"],
        "upstream_interfaces": definition["upstream_interfaces"],
        "limitations": definition["limitations"],
        "next_required": definition["next_required"],
        "checks": checks,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }


def write_report(report: dict[str, Any], path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default=None, help="Packet-core mode override.")
    parser.add_argument("--radio", type=Path, default=ROOT / "network/config/radio_24ghz.yaml")
    parser.add_argument(
        "--radio-backend-config",
        type=Path,
        default=ROOT / "network/config/radio_backend.yaml",
    )
    parser.add_argument("--endpoints", type=Path, default=ROOT / "network/config/endpoints.yaml")
    parser.add_argument("--ns3-dir", type=Path, default=Path(os.environ.get("NS3_DIR", ROOT / ".external/ns-3")))
    parser.add_argument("--purpose", choices=["evaluation", "runtime"], default="evaluation")
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--print-mode", action="store_true")
    parser.add_argument(
        "--skip-host-checks",
        action="store_true",
        help="Skip /dev/net/tun and ip-netns probes for deterministic config tests.",
    )
    args = parser.parse_args()

    try:
        report = build_mode_report(
            radio_config=load_yaml(args.radio),
            radio_backend=load_yaml(args.radio_backend_config),
            endpoints=load_yaml(args.endpoints),
            ns3_dir=args.ns3_dir,
            requested_mode=args.mode,
            purpose=args.purpose,
            skip_host_checks=args.skip_host_checks,
        )
    except (SystemExit, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2

    write_report(report, args.json_output)

    if args.print_mode:
        print(report["mode"])

    if args.purpose == "runtime" and not report["can_run"]:
        print(
            "FAIL packet-core mode "
            f"{report['mode']!r} is not runtime-selectable: {report['status']}",
            file=sys.stderr,
        )
        for check in report["missing_required"]:
            print(f"  - {check['name']}: {check['detail']}", file=sys.stderr)
        if not report["runtime_selectable"]:
            print("  - runtime implementation is intentionally fail-closed", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
