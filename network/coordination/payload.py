"""Payload-UART observations through the existing native-radio FlightHarness.

Only GCS HEARTBEAT and SET_MESSAGE_INTERVAL(ATTITUDE) are transmitted. Flight
control, mode changes and arming remain owned by the control-channel mission.
No direct SITL socket or fabricated telemetry is used.
"""
from __future__ import annotations

import math
from pathlib import Path

from network.scripts import simulation_clock as clock
from scripts.product.town01_full_stack_scenario import FlightHarness, write_json


class PayloadChannel:
    def __init__(self, run_dir: Path, node_state: Path, uav_ids: tuple[int, ...], *,
                 interval_s: float = .2, heartbeat_s: float = 1., request_s: float = 2.):
        for name, value in (('interval_s', interval_s), ('heartbeat_s', heartbeat_s), ('request_s', request_s)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if clock.mode() != 'realtime':
            raise ValueError('coordinator payload observations require realtime')
        self.run_dir = Path(run_dir)
        self.interval_s, self.heartbeat_s, self.request_s = interval_s, heartbeat_s, request_s
        self.harness = FlightHarness(self.run_dir / 'payload', Path(node_state), 1.,
                                     channels=('payload',), uav_ids=tuple(uav_ids))
        self.uav_ids = self.harness.uav_ids
        self.mav = self.harness.mavutil.mavlink
        self.started_ns = clock.monotonic_ns()
        self.next_heartbeat_ns = self.next_request_ns = 0
        self.closed = False
        self.heartbeats_sent = {i: 0 for i in self.uav_ids}
        self.requests_sent = {i: 0 for i in self.uav_ids}
        self.last_request_sent_ns = {i: 0 for i in self.uav_ids}
        self.accepted_acknowledgements = {i: 0 for i in self.uav_ids}
        self.last_ack_ns = {i: 0 for i in self.uav_ids}
        self.backpressure_events = 0

    def _observe(self):
        command = int(self.mav.MAV_CMD_SET_MESSAGE_INTERVAL)
        for system_id in self.uav_ids:
            entry = self.harness.acks.get(('payload', system_id, command))
            if entry is not None:
                message, observed_ns = entry
                if observed_ns > self.last_ack_ns[system_id]:
                    self.last_ack_ns[system_id] = observed_ns
                    if (self.last_request_sent_ns[system_id]
                            and observed_ns >= self.last_request_sent_ns[system_id]
                            and int(message.result) == int(self.mav.MAV_RESULT_ACCEPTED)):
                        self.accepted_acknowledgements[system_id] += 1

    def _send(self, system_id, message):
        try:
            self.harness.send('payload', system_id, message)
        except BlockingIOError:
            self.backpressure_events += 1
            return False
        return True

    def pump(self):
        """Nonblocking; call from the mission's existing event loop."""
        if self.closed:
            raise RuntimeError('payload channel is closed')
        self.harness.pump(0.)
        self._observe()
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
            self.next_request_ns = now + int(self.request_s * 1e9)

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
                    hardware_validation='not_performed', transport=self.harness.transport_summary())

    def close(self):
        if not self.closed:
            error = None
            try:
                self.harness.pump(0.)
                self._observe()
            except Exception as exc:
                error = exc
            try:
                self.harness.close()
            except Exception as exc:
                error = error or exc
            self.closed = True
            self.metrics = self.snapshot()
            if error is not None:
                self.metrics['close_error'] = f'{type(error).__name__}: {error}'
            write_json(self.run_dir / 'metrics/payload_channel.json', self.metrics)
            if error is not None:
                raise error
        return self.metrics
