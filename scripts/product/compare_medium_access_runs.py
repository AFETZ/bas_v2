#!/usr/bin/env python3
"""Write a factual comparison of separate stock and centralized ns-3 runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object required: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-run", type=Path, required=True)
    parser.add_argument("--centralized-run", type=Path, required=True)
    parser.add_argument("--output-run", type=Path, required=True)
    args = parser.parse_args()
    stock = read(args.stock_run / "metrics/medium_access_baseline.json")
    centralized_metadata = read(args.centralized_run / "metrics/medium_access_run.json")
    centralized_profiles = read(args.centralized_run / "metrics/traffic_profiles.json")
    properties = [
        ("Upstream patch", "no", "yes"),
        ("External global scheduler", "no", "yes"),
        ("Admission shaping", "no", "yes"),
        ("Natural stock backoff", "yes", "no, centralized grant"),
        ("Control protection", "not guaranteed", "configured"),
        ("Technology-specific PHY", "no", "no"),
        ("Real Sionna propagation", "yes", "yes"),
        ("Real endpoint packets", "yes", "yes"),
    ]
    output = args.output_run.resolve()
    (output / "metrics").mkdir(parents=True, exist_ok=True)
    value: dict[str, Any] = {
        "stock_run": str(args.stock_run.resolve()),
        "centralized_run": str(args.centralized_run.resolve()),
        "stock_metadata": stock["medium_access"],
        "centralized_metadata": centralized_metadata,
        "properties": [
            {"property": name, "stock_ns3_csma": stock_value, "centralized_priority_scheduler": centralized_value}
            for name, stock_value, centralized_value in properties
        ],
        "stock_profiles": stock["profiles"],
        "centralized_profiles": centralized_profiles,
        "interpretation": "The modes are different policies; no unqualified better/worse conclusion is made.",
    }
    (output / "metrics/medium_access_comparison.json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Medium-access comparison",
        "",
        f"- Stock run: `{args.stock_run.resolve()}`",
        f"- Centralized regression: `{args.centralized_run.resolve()}`",
        "",
        "| Property | stock_ns3_csma | centralized_priority_scheduler |",
        "| --- | --- | --- |",
    ]
    lines.extend(f"| {name} | {stock_value} | {centralized_value} |" for name, stock_value, centralized_value in properties)
    lines.extend([
        "",
        "The modes are intentionally not ranked without a stated criterion. Both use live Sionna RT state and real endpoint packets, while neither represents a technology-specific customer radio modem.",
        "",
    ])
    (output / "medium_access_comparison.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
