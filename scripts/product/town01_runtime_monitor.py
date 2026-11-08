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
    "native_rt_worker": ("ns3.48-native-rt-worker",),
    "uart_adapter": ("communication_vertical.py uart-adapter",),
    "scenario": ("town01_full_stack_scenario.py run",),
    "native_product_scenario": ("native_radio_product_scenario.py run",),
    "native_five_uav_scenario": ("native_radio_five_uav_scenario.py run",),
    "relay_scenario": ("scripts/product/relay_experiment.py",),
    "coordination_service": ("network.coordination.service",),
    "traffic_profile": ("town01_communication_profiles.py",),
}


def cpu_partition(allowed=None, cpu_root=Path('/sys/devices/system/cpu')):
    """Keep each physical core's allowed SMT threads in one BAS role.

    taskset still owns enforcement. This is a per-run layout, not host-wide
    CPU reservation; unrelated processes may continue using the same cores.
    """
    cpus = sorted(os.sched_getaffinity(0) if allowed is None else allowed)
    if not cpus or len(set(cpus)) != len(cpus) or any(type(i) is not int or i < 0 for i in cpus):
        raise ValueError('CPU affinity must contain unique nonnegative integer IDs')
    groups = {}
    for cpu in cpus:
        topology = cpu_root / f'cpu{cpu}' / 'topology'
        try:
            package = int((topology / 'physical_package_id').read_text().strip())
            core = int((topology / 'core_id').read_text().strip())
        except (OSError, ValueError) as error:
            raise ValueError(f'CPU {cpu}: physical topology unavailable: {error}') from error
        if min(package, core) < 0:
            raise ValueError(f'CPU {cpu}: unknown physical package/core')
        groups.setdefault((package, core), []).append(cpu)
    ordered = sorted(groups)
    shared = len(ordered) == 1
    boundary = min(8, max(1, len(ordered)//2))
    stack_keys = ordered if shared else ordered[:boundary]
    radio_keys = ordered if shared else ordered[boundary:]
    members = lambda keys: sorted(cpu for key in keys for cpu in groups[key])
    return dict(schema=1, method='shared_single_physical_core' if shared else 'whole_physical_cores',
        allowed_cpus=cpus, stack_cpus=members(stack_keys), radio_cpus=members(radio_keys),
        shared_physical_cores=shared, reservation='BAS process affinity only; no host-wide exclusivity',
        topology=[dict(package_id=package,core_id=core,allowed_cpus=groups[(package,core)])
                  for package,core in ordered])


def host_record(proc_root=Path('/proc')):
    """Clock and kernel-pressure evidence for stalls; missing probes stay null."""
    record = dict(component='host', monotonic_ns=time.monotonic_ns(), unix_ns=time.time_ns(),
                  boottime_ns=None, pressure={}, memory_available_bytes=None)
    if hasattr(time, 'CLOCK_BOOTTIME'):
        record['boottime_ns'] = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    for name in ('cpu', 'io', 'memory'):
        try:
            record['pressure'][name] = (proc_root / 'pressure' / name).read_text().strip()
        except OSError:
            record['pressure'][name] = None
    try:
        for line in (proc_root / 'meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                record['memory_available_bytes'] = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return record


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
            status = (directory / 'status').read_text(encoding='utf-8')
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
                "allowed_cpu_list": next((line.split(':',1)[1].strip() for line in status.splitlines()
                                          if line.startswith('Cpus_allowed_list:')), None),
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
    with args.output.open("w", encoding="utf-8") as output, args.output.with_name("runtime_host.jsonl").open("w", encoding="utf-8") as host_output:
        while not args.stop_file.exists():
            host_output.write(json.dumps(host_record(), sort_keys=True, separators=(",", ":")) + "\n")
            host_output.flush()
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
