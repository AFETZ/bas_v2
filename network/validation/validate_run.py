#!/usr/bin/env python3
"""Validate one network/radio run from raw evidence under the v3 contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import P0_GATE_IDS, evaluate_run  # noqa: E402

import yaml  # noqa: E402


P0_TITLES = {
    "dependency_check": "Dependency and runtime capability",
    "provenance": "Source/config provenance",
    "joint_runtime": "Joint runtime",
    "five_uav_health": "Five-UAV health",
    "packet_provenance": "Packet provenance",
    "no_bypass": "Active no-bypass",
    "three_traffic_classes": "Three traffic classes",
    "online_sionna": "Online Sionna",
    "sionna_causality": "Sionna affects real packets",
    "link_locality": "Link-local impairment",
    "shared_medium": "Shared-medium behavior",
    "priority": "Control priority",
    "jamming": "Jamming off/on/off",
    "time_coherence": "Time coherence",
    "scene_alignment": "Scene/frame alignment",
    "heatmaps": "Heatmaps",
    "artifacts": "Artifact structure",
    "repeatability": "Clean-clone repeatability",
}


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def markdown_table(gates: dict[str, dict[str, Any]], titles: dict[str, str] | None = None) -> list[str]:
    lines = ["| Gate | Status | Proof |", "| --- | --- | --- |"]
    for gate_id, item in gates.items():
        title = (titles or {}).get(gate_id, gate_id.replace("_", " ").title())
        proof = str(item.get("proof", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {title} | {str(item.get('status', 'unknown')).upper()} | {proof} |")
    return lines


def write_report(run_dir: Path, result: dict[str, Any]) -> Path:
    blockers = [
        f"{P0_TITLES.get(gate_id, gate_id)}: {item['status']} — {item['proof']}"
        for gate_id, item in result["gates"]["p0"].items()
        if item["status"] != "passed"
    ]
    blockers.extend(str(item) for item in result.get("contract_blockers", []))
    lines = [
        "# Network/Radio Validation Report v3",
        "",
        f"Generated UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Run directory: `{run_dir}`",
        f"Validation engine: `{result['validation_engine']}` / schema `{result['schema_version']}`",
        "",
        f"Customer-ready status: **{'ready' if result['customer_ready'] else 'not ready'}**.",
        "",
        "P0 is computed from raw evidence. Runtime/postprocessor booleans and file presence are not accepted as causal proof.",
        "",
        "## P0 Gates",
        "",
        *markdown_table(result["gates"]["p0"], P0_TITLES),
        "",
        "## P1 Gates",
        "",
        *markdown_table(result["gates"]["p1"]),
        "",
        "## Current P0 Blockers",
        "",
    ]
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Validation Rules",
            "",
            "- ARP-only or byte-copied class PCAP fails packet-path validation.",
            "- Zero RX, complete loss, or null mandatory latency fails baseline delivery.",
            "- Active no-bypass requires structured ns-3 on/stopped/recovery phases with live endpoints.",
            "- Self-reported `validation.*` fields are ignored for gate decisions.",
            "",
        ]
    )
    path = run_dir / "validation_report.md"
    atomic_write_text(path, "\n".join(lines))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=ROOT_DIR / "network/config/validation_matrix.yaml",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--long-run", choices=("optional", "required", "skip"), default="optional")
    parser.add_argument("--long-run-min-duration-s", type=float, default=1800.0)
    parser.add_argument("--no-write", action="store_true", help="Evaluate without changing the run directory.")
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"FAIL run directory does not exist: {run_dir}", file=sys.stderr)
        return 2

    matrix_path = args.matrix.resolve()
    authoritative_matrix_path = (ROOT_DIR / "network/config/validation_matrix.yaml").resolve()
    try:
        if matrix_path.read_bytes() != authoritative_matrix_path.read_bytes():
            raise ValueError(
                "acceptance matrix is not byte-identical to network/config/validation_matrix.yaml"
            )
        matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
        matrix_gate_ids = tuple(item["id"] for item in matrix["gates"]["p0"])
        if matrix.get("schema_version") != 2:
            raise ValueError("schema_version is not 2")
        if matrix.get("plan") != "doc/network_radio_integration_plan_v3.md":
            raise ValueError("matrix does not point to the authoritative v3 plan")
        if matrix_gate_ids != P0_GATE_IDS:
            raise ValueError(
                f"P0 gates differ from validator: matrix={matrix_gate_ids}, validator={P0_GATE_IDS}"
            )
    except Exception as exc:
        print(f"FAIL validation matrix is invalid: {exc}", file=sys.stderr)
        return 2

    result = evaluate_run(
        run_dir,
        long_run_minimum_s=args.long_run_min_duration_s,
        matrix_path=matrix_path,
    )
    profile_policy = matrix.get("acceptance_profiles")
    profile_policy_valid = (
        isinstance(profile_policy, dict)
        and profile_policy.get("schema") == "v3_profiled"
        and profile_policy.get("required_customer_profile") == "m8_customer_handoff"
        and profile_policy.get("customer_ready_enabled") is True
        and profile_policy.get("implementation_status") == "complete"
    )
    result["acceptance_profile_policy"] = profile_policy
    if not profile_policy_valid:
        result["contract_blockers"] = [
            "v3 profile-specific M7/M8 validators are fail-closed until the "
            "m8_customer_handoff policy is implemented and enabled"
        ]
        result["p0_passed"] = False
        result["customer_ready"] = False
    if args.long_run == "skip":
        result["gates"]["p1"]["long_run"] = {
            "status": "skipped",
            "proof": "long-run gate explicitly skipped",
        }
    required_long_run_failed = (
        args.long_run == "required"
        and result["gates"]["p1"]["long_run"]["status"] != "passed"
    )
    if not args.no_write:
        metrics_dir = run_dir / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        output = args.json_output.resolve() if args.json_output else metrics_dir / "validation_results.json"
        try:
            output.relative_to(metrics_dir.resolve())
        except ValueError:
            print("FAIL --json-output must stay under the run metrics directory", file=sys.stderr)
            return 2
        reserved_outputs = {
            "runtime_summary.json",
            "summary.json",
            "provenance.json",
            "evidence_manifest.json",
            "joint_runtime.json",
            "five_uav_health.json",
            "packet_provenance.json",
        }
        if output.name in reserved_outputs:
            print(f"FAIL --json-output collides with raw/reserved evidence: {output.name}", file=sys.stderr)
            return 2
        atomic_write_text(output, json.dumps(result, indent=2, sort_keys=True) + "\n")

        # Preserve runtime metrics and publish validation state without trusting
        # any previous gate booleans.
        summary = dict(result.get("runtime_metrics") or {})
        summary.update(
            {
                "run_id": run_dir.name,
                "p0_passed": result["p0_passed"],
                "customer_ready": result["customer_ready"],
                "validation_engine": {
                    "name": result["validation_engine"],
                    "schema_version": result["schema_version"],
                },
                "gates": result["gates"],
            }
        )
        atomic_write_text(
            metrics_dir / "summary.json",
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
        )
        report_path = write_report(run_dir, result)
        print(f"Validation report: {report_path}")
        print(f"Validation results: {output}")

    print(f"P0 passed: {str(result['p0_passed']).lower()}")
    if required_long_run_failed:
        print("Required P1 long-run gate did not pass")
    return 0 if result["p0_passed"] and not required_long_run_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
