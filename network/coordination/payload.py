"""Payload-UART observations through the existing native-radio FlightHarness.

The default sends GCS HEARTBEAT and SET_MESSAGE_INTERVAL(ATTITUDE). An optional
synthetic TUNNEL load uses the same framed radio/UART boundary. Its receipt is
UART OS-write completion, not flight-controller application acceptance.
"""
from __future__ import annotations

import math
import os
import hashlib
import json
from pathlib import Path

from network.scripts import simulation_clock as clock
from scripts.product.town01_full_stack_scenario import FlightHarness, write_json, append_jsonl
from network.scripts.serial_transport import mavlink_payload_lineage


LOAD_CLASSES = {'1kbps': (1000, 16), '10kbps': (10000, 64), '100kbps': (100000, 128),
                '500kbps': (500000, 128), '2mbps': (2000000, 128), '20mbps_plus': (20000000, 128)}


def normalize_payload_load(value, uav_ids):
    """One public configuration; rates are useful test bytes, not wire bitrate."""
    if value is None:
        return None
    allowed = {'class', 'uav_ids', 'direction', 'useful_bytes', 'useful_bitrate_bps'}
    if (not isinstance(value, dict) or set(value) - allowed or not isinstance(value.get('class'), str)
            or value['class'] not in LOAD_CLASSES):
        raise ValueError('payload_load requires a supported class and no unknown fields')
    name = value['class']
    roster = value.get('uav_ids', list(uav_ids))
    if (not isinstance(roster, list) or not roster or any(type(i) is not int or i not in uav_ids for i in roster)
            or len(set(roster)) != len(roster)):
        raise ValueError('payload_load.uav_ids must be unique IDs from the configured roster')
    if value.get('direction', 'gcs_to_uav') != 'gcs_to_uav':
        raise ValueError('payload_load currently supports gcs_to_uav only; no upstream application producer is qualified')
    size, rate = value.get('useful_bytes', LOAD_CLASSES[name][1]), value.get('useful_bitrate_bps', LOAD_CLASSES[name][0])
    if type(size) is not int or not 1 <= size <= 128:
        raise ValueError('payload_load.useful_bytes must be an integer from 1 to 128')
    if (type(rate) is not int or rate < LOAD_CLASSES[name][0]
            or name != '20mbps_plus' and rate != LOAD_CLASSES[name][0]):
        raise ValueError('payload_load.useful_bitrate_bps must match its decimal class (20mbps_plus permits >=20000000)')
    if rate > size * 8 * 1_000_000_000:
        raise ValueError('payload_load period must be at least one monotonic nanosecond')
    return {'class': name, 'uav_ids': sorted(roster), 'direction': 'gcs_to_uav',
            'useful_bytes': size, 'useful_bitrate_bps': rate}


def payload_load_from_json(text, uav_ids):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate payload_load field: ' + key)
            result[key] = value
        return result
    return normalize_payload_load(json.loads(text, object_pairs_hook=unique), uav_ids)


def payload_load_slots(profile, duration_ns):
    numerator = duration_ns * profile['useful_bitrate_bps']
    denominator = profile['useful_bytes'] * 8 * 1_000_000_000
    return (numerator + denominator - 1) // denominator


def payload_load_slot_ns(profile, start_ns, sequence):
    return start_ns + sequence * profile['useful_bytes'] * 8 * 1_000_000_000 // profile['useful_bitrate_bps']


class PayloadChannel:
    def __init__(self, run_dir: Path, node_state: Path, uav_ids: tuple[int, ...], *,
                 interval_s: float = .2, heartbeat_s: float = 1., request_s: float = 2.,
                 event_logging: str | None = None, load=None):
        for name, value in (('interval_s', interval_s), ('heartbeat_s', heartbeat_s), ('request_s', request_s)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if clock.mode() != 'realtime':
            raise ValueError('coordinator payload observations require realtime')
        self.event_logging = os.environ.get('BAS_NATIVE_EVENT_LOGGING', 'metrics_only') if event_logging is None else event_logging
        if self.event_logging not in ('metrics_only', 'batched_trace'):
            raise ValueError('event_logging must be metrics_only or batched_trace')
        self.load = normalize_payload_load(load, uav_ids)
        if self.load is not None and self.event_logging != 'batched_trace':
            raise ValueError('payload_load requires event_logging=batched_trace for per-offer evidence')
        self.load_next = 0
        self.load_counters = {i: dict(created=0, sent=0, skipped=0, outside_slot=0, send_failed=0,
                                     maximum_creation_lag_ns=0) for i in self.load['uav_ids']} if self.load else {}
        self._load_offer = None
        self.run_dir = Path(run_dir)
        self.interval_s, self.heartbeat_s, self.request_s = interval_s, heartbeat_s, request_s
        self.harness = FlightHarness(self.run_dir / 'payload', Path(node_state), 1.,
            channels=('payload',), uav_ids=tuple(uav_ids), observer=self._on_message,
            frame_observer=self._on_frame if self.event_logging == 'batched_trace' else None,
            send_observer=self._on_send if self.event_logging == 'batched_trace' else None)
        self.uav_ids = self.harness.uav_ids
        self.mav = self.harness.mavutil.mavlink
        self.started_ns = clock.monotonic_ns()
        self.next_heartbeat_ns = self.next_request_ns = 0
        self.closed = False
        self.heartbeats_sent = {i: 0 for i in self.uav_ids}
        self.requests_sent = {i: 0 for i in self.uav_ids}
        self.last_request_sent_ns = {i: 0 for i in self.uav_ids}
        self.accepted_acknowledgements = {i: 0 for i in self.uav_ids}
        self.backpressure_events = 0
        self.window = None
        self._lineage_event('payload_lineage_started', uav_ids=list(self.uav_ids))

    def _lineage_event(self, event, **fields):
        if self.event_logging == 'batched_trace':
            value = dict(schema=1, event=event, run_id=self.run_dir.name,
                process_epoch=f'gcs:{self.started_ns}', clock_domain='host_monotonic',
                observed_host_monotonic_ns=clock.monotonic_ns())
            value.update(fields)
            append_jsonl(self.harness.events_path, value)

    def _on_frame(self, channel, system_id, message, observed_ns, spans):
        lineage = mavlink_payload_lineage(message, spans, run_id=self.run_dir.name,
                                           uav_id=system_id, direction='uav_to_gcs')
        self._lineage_event('payload_frame_accepted', channel=channel, uav_id=system_id,
            direction='uav_to_gcs', endpoint='gcs_crc_parse_release',
            accepted_host_monotonic_ns=observed_ns, **lineage)

    def _on_send(self, stage, channel, system_id, message, created_ns, producer_ns, sequence, expected, sent, observed_ns):
        frame = bytes(message.get_msgbuf())
        lineage = mavlink_payload_lineage(message, [((sequence, producer_ns), 0, len(frame))],
            run_id=self.run_dir.name, uav_id=system_id, direction='gcs_to_uav')
        deadline_ns = created_ns + round(self.harness.qos['classes']['payload']['deadline_ms'] * 1e6)
        application = {}
        if self._load_offer is not None:
            application['application_load'] = dict(self._load_offer)
            stat = self.load_counters[system_id]
            if stage == 'offered':
                stat['created'] += 1
                scheduled = self._load_offer['scheduled_host_monotonic_ns']
                stat['maximum_creation_lag_ns'] = max(stat['maximum_creation_lag_ns'], created_ns - scheduled)
                next_slot = min(self.window['end_ns'], payload_load_slot_ns(self.load, self.window['start_ns'],
                                                                         self._load_offer['sequence'] + 1))
                stat['outside_slot'] += created_ns < scheduled or created_ns >= next_slot
            elif stage == 'sent':
                stat['sent'] += 1
            elif stage == 'send_failed':
                stat['send_failed'] += 1
        self._lineage_event('payload_frame_' + stage, channel=channel, uav_id=system_id,
            direction='gcs_to_uav', endpoint='gcs_payload_socket', created_host_monotonic_ns=created_ns,
            deadline_host_monotonic_ns=deadline_ns, expected_fragments=expected, sent_fragments=sent,
            observed_host_monotonic_ns=observed_ns, **lineage, **application)

    def start_measurement(self, start_ns: int, end_ns: int):
        """Observe one half-open host-monotonic window; preflight counts stay separate."""
        if self.closed or self.window is not None:
            raise RuntimeError('measurement window cannot be restarted or set after close')
        if type(start_ns) is not int or type(end_ns) is not int or not 0 <= start_ns < end_ns:
            raise ValueError('measurement bounds must be integer monotonic ns with start < end')
        self.window = dict(start_ns=start_ns, end_ns=end_ns, uavs={i: {
            name: dict(count=0, first_ns=None, last_ns=None, maximum_gap_ns=0)
            for name in ('interval_request', 'accepted_interval_ack', 'HEARTBEAT', 'ATTITUDE')}
            for i in self.uav_ids})
        if self.load:
            self._lineage_event('payload_load_started', load_id='payload_load', profile=self.load,
                start_host_monotonic_ns=start_ns, end_host_monotonic_ns=end_ns,
                planned_slots_per_uav=payload_load_slots(self.load, end_ns - start_ns))

    def _mark_window(self, name, system_id, observed_ns):
        if self.window is None or not self.window['start_ns'] <= observed_ns < self.window['end_ns']:
            return
        stat = self.window['uavs'][system_id][name]
        if stat['last_ns'] is not None:
            stat['maximum_gap_ns'] = max(stat['maximum_gap_ns'], observed_ns - stat['last_ns'])
        stat['count'] += 1
        stat['first_ns'] = observed_ns if stat['first_ns'] is None else stat['first_ns']
        stat['last_ns'] = observed_ns

    def _on_message(self, channel, system_id, message, observed_ns):
        """Called only after real BSF1 reassembly, MAVLink CRC parsing and SYSID checks."""
        if channel != 'payload':
            return
        kind = message.get_type()
        if kind in ('HEARTBEAT', 'ATTITUDE'):
            self._mark_window(kind, system_id, observed_ns)
        if (kind == 'COMMAND_ACK' and int(message.command) == int(self.mav.MAV_CMD_SET_MESSAGE_INTERVAL)
                and int(message.result) == int(self.mav.MAV_RESULT_ACCEPTED)
                and self.last_request_sent_ns[system_id]
                and observed_ns >= self.last_request_sent_ns[system_id]):
            self.accepted_acknowledgements[system_id] += 1
            if self.window is not None and self.window['start_ns'] <= self.last_request_sent_ns[system_id] < self.window['end_ns']:
                self._mark_window('accepted_interval_ack', system_id, observed_ns)

    def measurement_snapshot(self, now_ns=None):
        if self.window is None:
            return dict(status='not_started', all_bidirectional_observed=False, service_sla='not_evaluated')
        now_ns = clock.monotonic_ns() if now_ns is None else now_ns
        start, end = self.window['start_ns'], self.window['end_ns']
        until = max(start, min(now_ns, end))
        uavs = {}
        for system_id, events in self.window['uavs'].items():
            observations = {}
            for name, stat in events.items():
                first, last = stat['first_ns'], stat['last_ns']
                gap = until - start if first is None else max(first - start, stat['maximum_gap_ns'], until - last)
                observations[name] = dict(count=stat['count'], first_host_monotonic_ns=first,
                    last_host_monotonic_ns=last,
                    max_internal_gap_s=None if stat['count'] < 2 else stat['maximum_gap_ns'] / 1e9,
                    max_observed_gap_s_including_edges=gap / 1e9,
                    last_age_s_at_observed_until=None if last is None else (until - last) / 1e9)
            uavs[str(system_id)] = dict(observations=observations,
                bidirectional_exchange_observed=all(stat['count'] > 0 for stat in events.values()))
        return dict(status='closed' if now_ns >= end else 'incomplete' if self.closed else 'observing', clock_domain='host_monotonic',
                    start_host_monotonic_ns=start, end_host_monotonic_ns=end,
                    observed_until_host_monotonic_ns=until, service_sla='not_evaluated', uavs=uavs,
                    timestamp_scope='request send / GCS MAVLink parse-release time; no hardware timing claim',
                    all_bidirectional_observed=all(row['bidirectional_exchange_observed'] for row in uavs.values()))

    def _send(self, system_id, message, *, load_offer=None):
        self._load_offer = load_offer
        try:
            self.harness.send('payload', system_id, message)
        except BlockingIOError:
            self.backpressure_events += 1
            return False
        finally:
            self._load_offer = None
        return True

    def _skip_load(self, first, count, reason):
        if count:
            for stat in self.load_counters.values():
                stat['skipped'] += count
            self._lineage_event('payload_load_slots_skipped', load_id='payload_load',
                first_sequence=first, count=count, uav_ids=self.load['uav_ids'], reason=reason)

    def _pump_load(self, now):
        if self.load is None or self.window is None or now < self.window['start_ns']:
            return
        start, end = self.window['start_ns'], self.window['end_ns']
        planned = payload_load_slots(self.load, end - start)
        if now >= end:
            self._skip_load(self.load_next, planned - self.load_next, 'window_ended_before_generation')
            self.load_next = planned
            return
        due = min(planned, payload_load_slots(self.load, now - start + 1))
        if due <= self.load_next:
            return
        self._skip_load(self.load_next, due - self.load_next - 1, 'publisher_missed_slots')
        sequence, self.load_next = due - 1, due
        for i in self.load['uav_ids']:
            digest = hashlib.sha256(f'{self.run_dir.name}|{i}|{sequence}'.encode()).digest()
            data = (digest * 4)[:self.load['useful_bytes']]
            message = self.harness.transmitters[('payload', i)].tunnel_encode(
                i, 1, 0, len(data), list(data) + [0] * (128 - len(data)))
            self._send(i, message, load_offer=dict(load_id='payload_load', sequence=sequence,
                scheduled_host_monotonic_ns=payload_load_slot_ns(self.load, start, sequence),
                useful_bytes=len(data), useful_sha256=hashlib.sha256(data).hexdigest()))

    def load_snapshot(self, now=None):
        if self.load is None:
            return dict(status='disabled', configured=False, generator_eligible=None)
        now = clock.monotonic_ns() if now is None else now
        planned = payload_load_slots(self.load, self.window['end_ns'] - self.window['start_ns']) if self.window else 0
        eligible = bool(self.closed and self.window and now >= self.window['end_ns'] and self.load_next == planned
            and all(stat['created'] == stat['sent'] == planned and not any(stat[k] for k in ('skipped','outside_slot','send_failed'))
                    for stat in self.load_counters.values()))
        return dict(status='closed' if self.closed else 'recording', configured=True, profile=self.load,
            generator_eligible=eligible, planned_slots_per_uav=planned,
            per_uav={str(i): dict(stat) for i, stat in self.load_counters.items()},
            aggregate_requested_useful_bps=len(self.load['uav_ids']) * self.load['useful_bitrate_bps'],
            pacing='one latest due slot per pump; skipped slots remain in the planned denominator; no catch-up bursts',
            byte_definition='TUNNEL payload[:payload_length]; synthetic test bytes, not customer video',
            receipt_boundary='UART OS-write end; not flight-controller application acceptance',
            service_classes='not_evaluated_requires_qualified_load_and_series')

    def pump(self):
        """Nonblocking; call from the mission's existing event loop."""
        if self.closed:
            raise RuntimeError('payload channel is closed')
        self.harness.pump(0.)
        now = clock.monotonic_ns()
        if now >= self.next_heartbeat_ns:
            for system_id in self.uav_ids:
                transmitter = self.harness.transmitters[('payload', system_id)]
                message = transmitter.heartbeat_encode(self.mav.MAV_TYPE_GCS,
                    self.mav.MAV_AUTOPILOT_INVALID, 0, 0, self.mav.MAV_STATE_ACTIVE)
                if self._send(system_id, message):
                    self.heartbeats_sent[system_id] += 1
            self.next_heartbeat_ns = now + int(self.heartbeat_s * 1e9)
        if now >= self.next_request_ns:
            for system_id in self.uav_ids:
                transmitter = self.harness.transmitters[('payload', system_id)]
                message = transmitter.command_long_encode(system_id, 1,
                    self.mav.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                    float(self.mav.MAVLINK_MSG_ID_ATTITUDE), self.interval_s * 1e6, 0, 0, 0, 0, 0)
                if self._send(system_id, message):
                    self.requests_sent[system_id] += 1
                    self.last_request_sent_ns[system_id] = clock.monotonic_ns()
                    self._mark_window('interval_request', system_id, self.last_request_sent_ns[system_id])
            self.next_request_ns = now + int(self.request_s * 1e9)
        self._pump_load(clock.monotonic_ns())

    def snapshot(self):
        now = clock.monotonic_ns()
        uavs = {}
        for system_id in self.uav_ids:
            heartbeat = self.harness.message_counts[('payload', system_id, 'HEARTBEAT')]
            attitude = self.harness.message_counts[('payload', system_id, 'ATTITUDE')]
            last_attitude_ns = self.harness.latest_at_ns.get(('payload', system_id, 'ATTITUDE'))
            latest = self.harness.latest.get(('payload', system_id, 'ATTITUDE'))
            uavs[str(system_id)] = dict(
                heartbeats_sent=self.heartbeats_sent[system_id], interval_requests_sent=self.requests_sent[system_id],
                accepted_interval_acknowledgements=self.accepted_acknowledgements[system_id],
                heartbeats_received=heartbeat, attitudes_received=attitude,
                last_attitude_received_host_monotonic_ns=last_attitude_ns,
                last_attitude_age_s=None if last_attitude_ns is None else (now - last_attitude_ns) / 1e9,
                last_attitude_autopilot_time_boot_ms=None if latest is None else int(latest.time_boot_ms),
                bidirectional_exchange_observed=bool(self.requests_sent[system_id]
                    and self.accepted_acknowledgements[system_id] and heartbeat and attitude))
        return dict(schema_version=1, channel='payload', clock_domain='host_monotonic',
                    sampled_host_monotonic_ns=now, started_host_monotonic_ns=self.started_ns,
                    status='closed' if self.closed else 'running', uavs=uavs,
                    all_uavs_bidirectional_observed=all(row['bidirectional_exchange_observed'] for row in uavs.values()),
                    observation_scope='payload helper lifetime; not a flight or hardware acceptance',
                    socket_backpressure_events=self.backpressure_events,
                    measurement_window=self.measurement_snapshot(now),
                    offered_load=self.load_snapshot(now),
                    hardware_validation='not_performed', transport=self.harness.transport_summary(),
                    application_lineage=dict(event_logging=self.event_logging,
                        status=('gcs_boundary_trace_closed' if self.closed else 'recording_enabled')
                            if self.event_logging == 'batched_trace' else 'not_evaluated_logging_disabled',
                        path='payload/logs/scenario_events.jsonl', byte_definition='MAVLink wire body, excluding all framing; not semantic customer content',
                        service_classes='not_evaluated', sensor_creation_time='not_measured'))

    def close(self):
        if not self.closed:
            error = None
            try:
                self.harness.pump(0.)
            except Exception as exc:
                error = exc
            try:
                self.harness.close()
            except Exception as exc:
                error = error or exc
            self.closed = True
            if self.load and self.window:
                planned = payload_load_slots(self.load, self.window['end_ns'] - self.window['start_ns'])
                self._skip_load(self.load_next, planned - self.load_next, 'producer_closed_before_generation')
                self.load_next = planned
                self._lineage_event('payload_load_closed', load_id='payload_load', offered_load=self.load_snapshot())
            self._lineage_event('payload_lineage_closed', close_error=str(error) if error else None)
            self.metrics = self.snapshot()
            if error is not None:
                self.metrics['close_error'] = f'{type(error).__name__}: {error}'
            write_json(self.run_dir / 'metrics/payload_channel.json', self.metrics)
            if error is not None:
                raise error
        return self.metrics
