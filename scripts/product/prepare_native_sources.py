#!/usr/bin/env python3
"""Validate configured emitters and expand sweep events; no propagation model."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys
import yaml


def number(value, name, minimum=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (minimum is not None and value < minimum):
        raise ValueError(f"invalid {name}: {value}")
    return float(value)


def whole_period(duration, period):
    # ns-3.48 Time uses unchanged default 1 ns resolution. Fixed sub-ns
    # tolerance in seconds avoids admitting partial pulses on long schedules.
    ratio = duration / period
    if not math.isfinite(ratio):
        raise ValueError("source period count must be finite")
    count = round(ratio)
    if count < 1 or not math.isclose(duration, count * period, rel_tol=0, abs_tol=1e-10):
        raise ValueError("segment duration must be a positive whole number of generator periods")


def vector(value, length, name):
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{name} requires exactly {length} numeric coordinates")
    return [number(x, name) for x in value]


def prepare(config):
    if not isinstance(config, dict) or not {'sources'} <= set(config) <= {'sources', 'schema_version'}:
        raise ValueError("source configuration requires sources and optional schema_version only")
    if 'schema_version' in config and (type(config['schema_version']) is not int or config['schema_version'] != 1):
        raise ValueError("source schema_version must be integer 1")
    if not isinstance(config['sources'], list) or not config['sources']:
        raise ValueError("sources must be a nonempty list")
    segments = []
    ids = set()
    for source in config["sources"]:
        common = {'id', 'position_m', 'orientation_rad', 'pattern', 'gain_dbi', 'power_w',
                  'bandwidth_hz', 'start_s', 'stop_s', 'period_s', 'duty_cycle', 'mode', 'spectral_shape'}
        if not isinstance(source, dict) or source.get('mode') not in ('continuous', 'pulsed', 'sweep'):
            raise ValueError("source mode must be continuous, pulsed or sweep")
        mode = source['mode']
        expected = common | ({'sweep_hz', 'dwell_s'} if mode == 'sweep' else {'center_hz'})
        if set(source) != expected:
            raise ValueError(f"{mode} source requires exactly {sorted(expected)}")
        sid = source["id"]
        if not isinstance(sid, str) or sid in ids or not sid.replace("_", "").isalnum():
            raise ValueError("source id must be unique and alphanumeric")
        ids.add(sid)
        position = vector(source["position_m"], 3, "position_m")
        orientation = vector(source["orientation_rad"], 2, "azimuth/downtilt radians")
        if any(abs(angle) > math.pi for angle in orientation):
            raise ValueError("native UPA azimuth/downtilt must lie in [-pi,pi] radians")
        pattern = source["pattern"]
        if pattern not in ("iso", "tr38901"):
            raise ValueError("only upstream Sionna iso/tr38901 patterns are supported")
        start = number(source["start_s"], "start_s", 0)
        stop = number(source["stop_s"], "stop_s", 0)
        period = number(source["period_s"], "period_s", 1e-4)
        duty = number(source["duty_cycle"], "duty_cycle", 1e-6)
        bandwidth = number(source["bandwidth_hz"], "bandwidth_hz", 1)
        power = number(source["power_w"], "power_w", 1e-15)
        if any(value * 1e9 >= 2**63 for value in (start, stop, period)):
            raise ValueError("source times exceed native signed-64-bit nanosecond Time range")
        period_ticks = math.floor(period * 1e9 + .5)
        if not math.isclose(period, period_ticks / 1e9, rel_tol=0, abs_tol=math.ulp(period)):
            raise ValueError("source period must be representable at native 1 ns Time resolution")
        start_ticks, stop_ticks = (math.floor(value * 1e9 + .5) for value in (start, stop))
        if (stop_ticks - start_ticks) % period_ticks:
            raise ValueError("source duration must contain a whole number of native generator periods")
        if math.floor(period_ticks * duty + .5) < 1:
            raise ValueError("source pulse duration rounds to zero at native 1 ns Time resolution")
        if stop <= start or duty > 1:
            raise ValueError("invalid duration, duty cycle or mode")
        if mode == "continuous" and duty != 1:
            raise ValueError("continuous requires duty_cycle=1")
        if source["spectral_shape"] != "rectangular":
            raise ValueError("this reference supports rectangular PSD only")
        frequencies = source["sweep_hz"] if mode == "sweep" else [source["center_hz"]]
        dwell = number(source["dwell_s"], "dwell_s", period) if mode == "sweep" else stop-start
        if not isinstance(frequencies, list) or not 1 <= len(frequencies) <= 64:
            raise ValueError("sweep requires 1..64 frequencies")
        frequencies = [number(hz, "center_hz", bandwidth/2+1) for hz in frequencies]
        gain = number(source["gain_dbi"], "gain_dbi")
        try:
            effective_power = power * 10 ** (gain / 10)
        except OverflowError:
            effective_power = math.inf
        if not math.isfinite(effective_power) or effective_power <= 0:
            raise ValueError("power after feed gain must be finite and positive")
        for hz in frequencies:
            if not 0 < hz - bandwidth/2 < hz < hz + bandwidth/2 < math.inf:
                raise ValueError("source spectrum bounds must be finite, positive and distinct")
        whole_period(stop - start, period)
        t, index = start, 0
        while t < stop-1e-9:
            end = min(t+dwell, stop)
            # WaveformGenerator.Stop cancels the next pulse, not an already
            # emitted wave. Whole periods guarantee no signal after stop_s.
            whole_period(end - t, period)
            if (math.floor(end * 1e9 + .5) - math.floor(t * 1e9 + .5)) % period_ticks:
                raise ValueError("segment duration must contain a whole number of native generator periods")
            hz = frequencies[index % len(frequencies)]
            segments.append(dict(id=sid, position_m=position, orientation_rad=orientation,
                pattern=pattern, gain_dbi=gain,
                center_hz=hz, bandwidth_hz=bandwidth, power_w=power,
                start_s=t, stop_s=end, period_s=period, duty_cycle=duty, mode=mode))
            if len(segments) > 512:
                raise ValueError("source schedule exceeds 512 segments")
            t, index = end, index+1
    return {"schema_version": 1, "clock": "ns3_simulation_seconds",
            "propagation": "native Sionna RT at each source center frequency",
            "segments": segments}


class UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate/malformed fields instead of silently overriding inputs."""
    def construct_mapping(self, node, deep=False):
        result = {}
        for key, value in self.construct_pairs(node, deep=deep):
            if not isinstance(key, str) or key in result:
                raise ValueError(f"duplicate or non-string source field: {key}")
            result[key] = value
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = prepare(yaml.load(args.config.read_text(encoding='utf-8-sig'), Loader=UniqueSafeLoader))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, allow_nan=False)+"\n", encoding='utf-8')


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"Native sources: {error}", file=sys.stderr)
        raise SystemExit(2)
