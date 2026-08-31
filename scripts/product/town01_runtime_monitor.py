#!/usr/bin/env python3
"""Lightweight CPU/RSS/GPU sampler for the integrated Town01 runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


MATCHES = {
    "arducopter": ("arducopter",),
    "gazebo": ("gz sim", "ruby /opt/ros/humble/opt/gz_tools_vendor/bin/gz sim"),
    "sionna_provider": ("network/radio_provider/provider.py",),
    "sionna_state_adapter": ("town01_radio_state.py",),
    "ns3_packet_engine": ("ams-tap-packet-engine",),
    "native_ns3_sionna": ("ns3.48-upstream-sionna-tap-spike",),
    "uart_adapter": ("communication_vertical.py uart-adapter",),
    "scenario": ("town01_full_stack_scenario.py run",),
    "native_product_scenario": ("native_radio_product_scenario.py run",),
    "native_five_uav_scenario": ("native_radio_five_uav_scenario.py run",),
    "traffic_profile": ("town01_communication_profiles.py",),
}


def process_records() -> list[dict[str, Any]]:
    page_size = os.sysconf("SC_PAGE_SIZE")
    records: list[dict[str, Any]] = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            command = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
            stat = (directory / "stat").read_text(encoding="utf-8").split()
            statm = (directory / "statm").read_text(encoding="utf-8").split()
        except (OSError, IndexError):
            continue
        component = next(
            (
                name
                for name, needles in MATCHES.items()
                if any(needle in command for needle in needles)
            ),
            None,
        )
        if component is None:
            continue
        records.append(
            {
                "pid": int(directory.name),
                "component": component,
                "cpu_ticks": int(stat[13]) + int(stat[14]),
                "rss_bytes": int(statm[1]) * page_size,
                "command": command[:500],
            }
        )
    return records


def gpu_memory() -> dict[int, int]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    values: dict[int, int] = {}
    if result.returncode:
        return values
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            values[int(parts[0])] = int(parts[1]) * 1024 * 1024
        except ValueError:
            continue
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--period-s", type=float, default=1.0)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous: dict[int, tuple[int, int]] = {}
    clock_ticks = os.sysconf("SC_CLK_TCK")
    with args.output.open("w", encoding="utf-8") as output:
        while not args.stop_file.exists():
            now_ns = time.monotonic_ns()
            gpu = gpu_memory()
            for record in process_records():
                pid = int(record["pid"])
                cpu_percent: float | None = None
                if pid in previous:
                    previous_ticks, previous_ns = previous[pid]
                    elapsed_s = (now_ns - previous_ns) / 1e9
                    if elapsed_s > 0:
                        cpu_percent = max(
                            0.0,
                            (int(record["cpu_ticks"]) - previous_ticks)
                            / clock_ticks
                            / elapsed_s
                            * 100.0,
                        )
                previous[pid] = (int(record["cpu_ticks"]), now_ns)
                record.update(
                    {
                        "monotonic_ns": now_ns,
                        "cpu_percent_one_core": cpu_percent,
                        "gpu_memory_bytes": gpu.get(pid),
                    }
                )
                output.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            output.flush()
            time.sleep(max(0.1, args.period_s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
