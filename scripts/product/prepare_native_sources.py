#!/usr/bin/env python3
"""Validate configured emitters and expand sweep events; no propagation model.

Source gain_dbi is additional feed gain in dB, not total antenna peak gain.
native-spectrum-sources.h applies power_w * 10**(gain_dbi/10) / bandwidth_hz
to the source PSD; its outer Spectrum antenna stays isotropic 0 dBi. Sionna's
tr38901 OUTDOOR pattern separately includes an 8 dBi peak, 65/65 degree beams
and 30 dB Amax/SLA. Thus power_w=1, gain_dbi=0 yields peak EIRP about 6.31 W;
gain_dbi=8 adds another 8 dB, rather than specifying that existing peak.
The SDK GetAntennaElementPattern/MakePlannarArray adapter selects iso/tr38901;
INDOOR/custom settings are not exported. This is not a Wi-Fi TX antenna field.
"""
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
    if not isinstance(config, dict) or not {'sources'} <= set(config) <= {'sources', 'schema_version', 'time_reference'}:
        raise ValueError("source configuration requires sources and optional schema_version/time_reference only")
    if 'schema_version' in config and (type(config['schema_version']) is not int or config['schema_version'] != 1):
        raise ValueError("source schema_version must be integer 1")
    if not isinstance(config['sources'], list) or not config['sources']:
        raise ValueError("sources must be a nonempty list")
    reference = config.get('time_reference', 'simulation_start')
    if reference not in ('simulation_start', 'measurement_start'):
        raise ValueError('time_reference must be simulation_start or measurement_start')
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
    return {"schema_version": 1, "clock": ("ns3_simulation_seconds" if reference == 'simulation_start'
                                           else "measurement_elapsed_seconds"),
            "propagation": "native Sionna RT at each source center frequency",
            "segments": segments}


def materialize(resolved, simulation_ns, host_monotonic_ns, phase):
    """Called by native simulator at its observed anchor; never invent an epoch.

    Integer nanoseconds returned here also feed Simulator::Schedule, so the
    exported intervals describe the actual native ticks rather than rounded
    estimates of bootstrap duration. An unobserved measurement phase has no
    absolute schedule and must not produce an *_actual.json file.
    """
    if (not isinstance(resolved, dict) or type(resolved.get('schema_version')) is not int
            or resolved.get('schema_version') != 1
            or resolved.get('clock') not in ('ns3_simulation_seconds', 'measurement_elapsed_seconds')
            or not isinstance(resolved.get('segments'), list) or not resolved['segments']
            or 'anchor' in resolved):
        raise ValueError('expected an unresolved source schedule with a supported clock')
    if (type(simulation_ns) is not int or not 0 <= simulation_ns < 2**63
            or type(host_monotonic_ns) is not int or host_monotonic_ns < 0
            or not isinstance(phase, str) or not phase):
        raise ValueError('anchor requires native simulation/host integer ns and an observed phase')
    relative = resolved['clock'] == 'measurement_elapsed_seconds'
    if relative and phase != 'experiment_normal':
        return None
    if not relative and simulation_ns != 0:
        raise ValueError('simulation_start schedule must be armed at native time zero')
    actual = dict(resolved, clock='ns3_simulation_seconds', input_clock=resolved['clock'],
                  anchor=dict(phase=phase, simulation_s=simulation_ns / 1e9,
                              simulation_ns=simulation_ns, host_monotonic_ns=host_monotonic_ns), segments=[])
    for source in resolved['segments']:
        item = dict(source)
        for field in ('start', 'stop', 'period'):
            seconds = number(source[field + '_s'], field + '_s', 0)
            ticks = math.floor(seconds * 1e9 + .5)
            if relative and field != 'period':
                ticks += simulation_ns
            if ticks >= 2**63:
                raise ValueError('anchored source time exceeds native signed-64-bit nanoseconds')
            item[field + '_ns'] = ticks
            item[field + '_s'] = ticks / 1e9
        if item['period_ns'] <= 0 or item['stop_ns'] <= item['start_ns']:
            raise ValueError('anchored source interval must be nonempty with a positive period')
        if (item['stop_ns'] - item['start_ns']) % item['period_ns']:
            raise ValueError('anchored interval does not contain whole native periods')
        actual['segments'].append(item)
    return actual


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
