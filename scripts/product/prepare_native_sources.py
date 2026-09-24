#!/usr/bin/env python3
"""Validate configured emitters and expand sweep events; no propagation model."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import yaml


def number(value, name, minimum=None):
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"invalid {name}: {value}")
    return result


def prepare(config):
    segments = []
    ids = set()
    for source in config["sources"]:
        sid = str(source["id"])
        if sid in ids or not sid.replace("_", "").isalnum():
            raise ValueError("source id must be unique and alphanumeric")
        ids.add(sid)
        position = [number(x, "position") for x in source["position_m"]]
        orientation = [number(x, "orientation") for x in source["orientation_rad"]]
        if len(position) != 3 or len(orientation) != 2:
            raise ValueError("position has 3 axes; orientation is azimuth/downtilt radians")
        pattern = source["pattern"]
        if pattern not in ("iso", "tr38901"):
            raise ValueError("only upstream Sionna iso/tr38901 patterns are supported")
        start = number(source["start_s"], "start_s", 0)
        stop = number(source["stop_s"], "stop_s", 0)
        period = number(source["period_s"], "period_s", 1e-4)
        duty = number(source["duty_cycle"], "duty_cycle", 1e-6)
        bandwidth = number(source["bandwidth_hz"], "bandwidth_hz", 1)
        power = number(source["power_w"], "power_w", 1e-15)
        mode = source["mode"]
        if stop <= start or duty > 1 or mode not in ("continuous", "pulsed", "sweep"):
            raise ValueError("invalid duration, duty cycle or mode")
        if mode == "continuous" and duty != 1:
            raise ValueError("continuous requires duty_cycle=1")
        if source["spectral_shape"] != "rectangular":
            raise ValueError("this reference supports rectangular PSD only")
        frequencies = source["sweep_hz"] if mode == "sweep" else [source["center_hz"]]
        dwell = number(source["dwell_s"], "dwell_s", period) if mode == "sweep" else stop-start
        if not frequencies or len(frequencies) > 64:
            raise ValueError("sweep requires 1..64 frequencies")
        t, index = start, 0
        while t < stop-1e-9:
            end = min(t+dwell, stop)
            # WaveformGenerator.Stop cancels the next pulse, not an already
            # emitted wave. Whole periods guarantee no signal after stop_s.
            if not math.isclose((end-t)/period, round((end-t)/period), abs_tol=1e-7):
                raise ValueError("segment duration must be a whole number of generator periods")
            hz = number(frequencies[index % len(frequencies)], "center_hz", bandwidth/2+1)
            segments.append(dict(id=sid, position_m=position, orientation_rad=orientation,
                pattern=pattern, gain_dbi=number(source["gain_dbi"], "gain_dbi"),
                center_hz=hz, bandwidth_hz=bandwidth, power_w=power,
                start_s=t, stop_s=end, period_s=period, duty_cycle=duty, mode=mode))
            if len(segments) > 512:
                raise ValueError("source schedule exceeds 512 segments")
            t, index = end, index+1
    return {"schema_version": 1, "clock": "ns3_simulation_seconds",
            "propagation": "native Sionna RT at each source center frequency",
            "segments": segments}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = prepare(yaml.safe_load(args.config.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2)+"\n")


if __name__ == "__main__":
    main()
