"""Live-only native-radio relay endpoints; run one process per namespace.

Commands preserve complete MAVLink bytes. UART telemetry reuses BSF1. The only
loopback sockets are the operator's local GCS ports; all grant/probe/command/
telemetry hops bind the native 10.71.N.10 addresses. No authentication against
malicious processes is claimed. A fresh run_id is mandatory on process restart.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import sys
import time
import tty
from typing import Any

from .policy import (AdmissionGate, Authority, Command, Grant, MAX_DATAGRAM_BYTES,
                     ProtocolError, UARTAdmission, decode, encode, make_command)
from network.scripts.serial_transport import Encoder, Reassembler, decode_chunk, radio_is_live


UAV_IDS = tuple(range(1, 6))


def endpoint_ip(index: int) -> str:
    return f"10.71.{index}.10"


class Events:
    def __init__(self, path: Path, run_id: str, uav_id: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a", buffering=1, encoding="utf-8")
        self.run_id, self.uav_id = run_id, uav_id

    def __call__(self, event: str, *, command: Command | None = None, **fields):
        value = dict(run_id=self.run_id, event=event, stage=event,
                     host_monotonic_ns=time.monotonic_ns(), clock_domain="host_monotonic",
                     source_sim_time_s=None, uav_id=self.uav_id, node_id=self.uav_id)
        if command is not None:
            value.update(leader_id=command.leader_id, epoch=command.epoch,
                         grant_sequence=command.grant_sequence,
                         valid_from_ns=command.valid_from_ns, valid_until_ns=command.valid_until_ns,
                         uav_id=command.destination_id,
                         command_id=command.command_id, sequence=command.sequence,
                         command_created_host_monotonic_ns=command.created_ns,
                         destination_id=command.destination_id,
                         payload_sha256=hashlib.sha256(command.payload).hexdigest(),
                         target_position_m=command.target_position_m,
                         target_frame=command.target_frame)
        value.update(fields)
        self.stream.write(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")


class Network:
    def __init__(self, args, index: int, emit: Events):
        self.args, self.index, self.emit = args, index, emit
        self.selector = selectors.DefaultSelector()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 184)
        self.udp.bind((endpoint_ip(index), args.port))
        self.udp.setblocking(False)
        self.selector.register(self.udp, selectors.EVENT_READ, "radio")
        self.peer_echo_ns: dict[int, int] = {}
        self.peer_rtt_ns: dict[int, int] = {}
        self.probes: dict[int, list[int]] = {}
        self.last_probe_ns = 0
        self.watchdog_seen_healthy = False
        self.running = True
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def stop(self, *_):
        self.running = False

    def guard_radio(self):
        """After native readiness, missing heartbeat is a failure too."""
        path = self.args.radio_watchdog_file
        if path is None:
            return
        healthy = radio_is_live(path, self.args.watchdog_s)
        if healthy:
            self.watchdog_seen_healthy = True
        elif self.watchdog_seen_healthy:
            raise RuntimeError("native radio watchdog expired or disappeared after readiness")

    def send(self, kind, body, destination: int):
        self.guard_radio()
        raw = encode(kind, self.index, body)
        try:
            self.udp.sendto(raw, (endpoint_ip(destination), self.args.port))
        except BlockingIOError:
            self.emit("network_send_drop", kind=kind, destination_id=destination,
                      reason="kernel_udp_backpressure")
            return False
        return True

    def periodic(self, now: int):
        if now - self.last_probe_ns < int(self.args.heartbeat_s * 1e9):
            return
        self.last_probe_ns = now
        for peer in (UAV_IDS if self.index == 0 else (0, *UAV_IDS)):
            if peer == self.index:
                continue
            values = self.probes.setdefault(peer, [])
            values.append(now)
            del values[:-16]
            sent = self.send("probe", dict(run_id=self.args.run_id, sent_ns=now), peer)
            if self.index == 0:
                self.emit("gcs_probe_issued", destination_id=peer,
                          probe_sent_host_monotonic_ns=now, send_ok=sent)

    def receive(self):
        raw, source = self.udp.recvfrom(MAX_DATAGRAM_BYTES + 1)
        kind, sender, body = decode(raw)
        if source != (endpoint_ip(sender), self.args.port):
            raise ProtocolError("source address disagrees with protocol sender")
        run = body.run_id if isinstance(body, (Grant, Command)) else body.get("run_id")
        if run != self.args.run_id:
            raise ProtocolError("foreign run")
        now = time.monotonic_ns()
        if kind == "probe":
            sent = body.get("sent_ns")
            if type(sent) is not int or not 0 <= sent <= now:
                raise ProtocolError("invalid probe timestamp")
            self.send("heartbeat", dict(run_id=self.args.run_id, echo_ns=sent,
                      sent_ns=now, peer_echo_ns=self.peer_echo_ns), sender)
            return None
        if kind == "heartbeat":
            echoed = body.get("echo_ns")
            pending = self.probes.get(sender, [])
            if type(echoed) is not int or echoed not in pending:
                raise ProtocolError("unsolicited or replayed probe response")
            pending.remove(echoed)
            self.peer_echo_ns[sender] = now
            self.peer_rtt_ns[sender] = now - echoed
        return kind, sender, body

    def close(self):
        self.selector.close()
        self.udp.close()


def configure_uart(path: str) -> int:
    # Reuse the existing checked baud setup without changing that adapter.
    script_path = str(Path(__file__).resolve().parents[1] / "scripts")
    if script_path not in sys.path:
        sys.path.insert(0, script_path)
    from communication_vertical import configure_tty_baud
    descriptor = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(descriptor)
    configure_tty_baud(descriptor, 115200)
    return descriptor


def mavlink_api():
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink import mavutil
    return mavutil


class UAVService(Network):
    def __init__(self, args, emit):
        super().__init__(args, args.uav_id, emit)
        self.uart = configure_uart(args.tty)
        self.selector.register(self.uart, selectors.EVENT_READ, "uart")
        self.mavutil = mavlink_api()
        self.mav = self.mavutil.mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
        self.gate = AdmissionGate(args.run_id, self.index, now_ns=time.monotonic_ns,
                                  hold=self.request_hold, emit=emit,
                                  max_command_age_ns=int(args.command_max_age_ms * 1e6))
        self.admission = UARTAdmission(self.gate, self.write_uart, emit=emit)
        self.encoder = Encoder(channel="control", uav_id=self.index, direction="uart_to_gcs")
        self.hold_pending: bytes | None = None
        self.last_hold_ns = 0
        self.hold_confirmed = True
        self.uart_parser = self.mavutil.mavlink.MAVLink(None)
        self.uart_parser.robust_parsing = True

    def write_uart(self, data: bytes) -> int:
        self.guard_radio()
        return os.write(self.uart, data)

    def request_hold(self):
        modes = {name: number for number, name in self.mavutil.mode_mapping_acm.items()}
        if "BRAKE" not in modes:
            raise RuntimeError("pinned ArduCopter dialect has no BRAKE mode mapping")
        message = self.mav.set_mode_encode(self.index,
                    self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, modes["BRAKE"])
        self.hold_pending = message.pack(self.mav)
        self.mav.seq = (self.mav.seq + 1) % 256
        self.hold_confirmed = False
        self.last_hold_ns = time.monotonic_ns()

    def write_hold(self):
        if self.hold_pending is None:
            return
        if self.admission.queue and self.admission.queue[0][2]:
            raise RuntimeError("cannot insert HOLD into a partially written command")
        try:
            size = self.write_uart(self.hold_pending)
        except BlockingIOError:
            return
        if size != len(self.hold_pending):
            raise RuntimeError("partial native HOLD write")
        self.emit("hold_uart_written", bytes=size, requested_mode="BRAKE")
        self.hold_pending = None

    def command(self, sender: int, command: Command):
        active = self.gate.active_grant()
        if sender == 0:
            if active is None or active.leader_id != self.index or command.leader_id != self.index:
                raise ProtocolError("GCS execution bypass to non-coordinator")
            # Verify received GCS bytes against the master's own grant before forwarding.
            self.gate.check_authority(command)
            self.emit("master_receive", command=command)
            if command.destination_id != self.index:
                if self.send("command", command, command.destination_id):
                    self.emit("master_forward", command=command)
                return
            sender = self.index
        self.emit("follower_receive", command=command)
        self.admission.enqueue(command, sender_id=sender)

    def telemetry(self, raw: bytes):
        now = time.monotonic_ns()
        self.emit("uart_observed", bytes=len(raw), payload_sha256=hashlib.sha256(raw).hexdigest())
        for message in self.uart_parser.parse_buffer(raw) or []:
            if message.get_type() == "HEARTBEAT":
                mode = self.mavutil.mode_string_v10(message)
                self.emit("autopilot_mode_observed", custom_mode=int(message.custom_mode), mode=mode)
                if mode == "BRAKE" and not self.hold_confirmed:
                    self.hold_confirmed = True
                    self.emit("hold_mode_observed", mode=mode)
        active = self.gate.active_grant()
        if active is None:
            return
        for datagram in self.encoder.encode(raw, sent_monotonic_ns=now):
            body = dict(run_id=self.args.run_id, origin_id=self.index,
                        leader_id=active.leader_id, epoch=active.epoch,
                        grant_sequence=active.grant_sequence, producer_ns=now, payload=datagram)
            self.send("telemetry", body, 0 if active.leader_id == self.index else active.leader_id)

    def handle(self, packet):
        if packet is None:
            return
        kind, sender, body = packet
        if kind == "grant":
            self.gate.receive_grant(body, sender_id=sender)
            if sender == 0 and body.leader_id == self.index:
                self.emit("grant_master_receive", leader_id=self.index, epoch=body.epoch,
                          grant_sequence=body.grant_sequence)
                # Essential for the former master whose direct GCS link is down.
                # The original issuer, sequence and absolute validity stay unchanged.
                for i in UAV_IDS:
                    if i != self.index and self.send("grant", body, i):
                        self.emit("grant_master_forward", destination_id=i,
                                  leader_id=self.index, epoch=body.epoch,
                                  grant_sequence=body.grant_sequence)
        elif kind == "command":
            self.command(sender, body)
        elif kind == "telemetry":
            active = self.gate.active_grant()
            grant = self.gate.grants.get(body.get("grant_sequence"))
            producer = body.get("producer_ns")
            now = time.monotonic_ns()
            if (active is None or active.leader_id != self.index or body.get("epoch") != active.epoch
                    or body.get("leader_id") != self.index or body.get("origin_id") != sender
                    or grant is None or grant.epoch != active.epoch or grant.leader_id != self.index
                    or type(producer) is not int
                    or not grant.valid_from_ns <= producer <= now < grant.valid_until_ns):
                raise ProtocolError("telemetry relay role mismatch")
            if self.send("telemetry", body, 0):
                self.emit("relay_return", uav_id=sender, origin_id=sender,
                          leader_id=self.index, epoch=active.epoch)

    def step(self):
        self.guard_radio()
        now = time.monotonic_ns()
        self.periodic(now)
        self.gate.tick()
        if (not self.hold_confirmed and self.gate.active_grant() is None
                and now - self.last_hold_ns >= 500_000_000):
            self.request_hold()  # retry native request until observed mode; no fake success
        self.write_hold()
        for _ in range(64):
            if not self.admission.drain_one():
                break
        for key, _ in self.selector.select(.01):
            if key.data == "radio":
                try:
                    self.handle(self.receive())
                except ProtocolError as exc:
                    self.emit("packet_rejected", reason=str(exc))
            else:
                raw = os.read(self.uart, 4096)
                if not raw:
                    raise RuntimeError("UART closed")
                self.telemetry(raw)

    def close(self):
        os.close(self.uart)
        super().close()


class GCSService(Network):
    def __init__(self, args, emit):
        super().__init__(args, 0, emit)
        self.authority = Authority(args.run_id, skew_margin_ns=int(args.skew_margin_s * 1e9))
        self.grant: Grant | None = None
        self.grants_by_epoch: dict[int, Grant] = {}
        self.issued_grants: dict[int, Grant] = {}
        self.last_renew_ns = 0
        self.detected_unavailable_leader = None
        self.heartbeat: dict[int, tuple[int, dict]] = {}
        self.sequence = 0
        self.clients, self.client_peers, self.parsers = {}, {}, {}
        self.receivers = {i: Reassembler(channel="control", uav_id=i, direction="uart_to_gcs",
                                         max_age_ms=args.telemetry_max_age_ms)
                          for i in UAV_IDS}
        self.telemetry_provenance = {i: {} for i in UAV_IDS}
        self.telemetry_scope = {}
        self.stream_provenance = {i: deque() for i in UAV_IDS}
        self.mavutil = mavlink_api()
        self.transmitter = self.mavutil.mavlink.MAVLink(None, srcSystem=255, srcComponent=190)
        self.uart_parsers = {}
        self.latest_messages: dict[int, dict[str, Any]] = {i: {} for i in UAV_IDS}
        self.latest_message_ns: dict[int, dict[str, int]] = {i: {} for i in UAV_IDS}
        self.latest_message_received_ns: dict[int, dict[str, int]] = {i: {} for i in UAV_IDS}
        for i in UAV_IDS:
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.bind(("127.0.0.1", 14550 + i))
            client.setblocking(False)
            self.clients[i] = client
            self.selector.register(client, selectors.EVENT_READ, i)
            self.parsers[i] = self.mavutil.mavlink.MAVLink(None)
            self.parsers[i].robust_parsing = True
            self.uart_parsers[i] = self.mavutil.mavlink.MAVLink(None)
            self.uart_parsers[i].robust_parsing = True

    def direct_available(self, i: int, now: int) -> bool:
        report = self.heartbeat.get(i)
        if report is None:
            return False
        received, body = report
        deadline = int(self.args.detection_s * 1e9)
        return all(type(stamp) is int and 0 <= now - stamp <= deadline
                   for stamp in (received, body.get("sent_ns"), body.get("echo_ns")))

    def available(self, now: int) -> list[int]:
        deadline = int(self.args.detection_s * 1e9)
        candidates = []
        for i, (received, body) in self.heartbeat.items():
            peer = body.get("peer_echo_ns", {})
            if not self.direct_available(i, now):
                continue
            if all(type(peer.get(str(j))) is int and 0 <= now - peer[str(j)] <= deadline
                   for j in UAV_IDS if j != i):
                candidates.append(i)
        return sorted(candidates, key=lambda i: (self.peer_rtt_ns.get(i, 1 << 63), i))

    def update_role(self, now: int):
        candidates = self.available(now)
        leader = self.authority.leader_id
        if leader is None:
            if self.args.initial_master not in candidates:
                return
            leader = self.args.initial_master
        elif not self.direct_available(leader, now):
            if self.detected_unavailable_leader != leader:
                self.emit("link_failure_detected", leader_id=leader,
                          reason="direct_GCS_probe_timeout")
                self.detected_unavailable_leader = leader
            if self.args.policy == "fixed" or not candidates:
                return
            leader = candidates[0]
        else:
            self.detected_unavailable_leader = None
        if leader == self.authority.leader_id and now - self.last_renew_ns < int(self.args.renew_s * 1e9):
            return
        previous = self.authority.leader_id
        self.grant = self.authority.issue(leader, now, int(self.args.lease_s * 1e9))
        self.grants_by_epoch[self.grant.epoch] = self.grant
        self.issued_grants[self.grant.grant_sequence] = self.grant
        self.last_renew_ns = now
        self.emit("grant_issued", leader_id=leader, epoch=self.grant.epoch,
                  grant_sequence=self.grant.grant_sequence,
                  valid_from_ns=self.grant.valid_from_ns, valid_until_ns=self.grant.valid_until_ns,
                  reason="selection" if previous != leader else "renewal")
        for i in UAV_IDS:
            self.send("grant", self.grant, i)

    def dispatch_frame(self, destination_id: int, payload: bytes, *,
                       target_position_m=None, target_frame=None,
                       resume_guided: bool = False) -> Command:
        """Programmatic GCS API: one real frame, optional declared target metadata.

        The caller services step() regularly. A mission must explicitly request
        resume_guided after expiry/BRAKE before its new setpoint is dispatched.
        Both mode restoration and setpoint still take the two native radio hops.
        This method returns a sent command, never an autopilot acknowledgement.
        """
        now = time.monotonic_ns()
        grant = self.grant
        if grant is None or not grant.valid_from_ns <= now < grant.valid_until_ns:
            raise ProtocolError("no_active_issued_lease")
        if resume_guided:
            modes = {name: number for number, name in self.mavutil.mode_mapping_acm.items()}
            mav = self.transmitter
            message = mav.set_mode_encode(destination_id,
                        self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, modes["GUIDED"])
            native_frame = message.pack(mav)
            mav.seq = (mav.seq + 1) % 256
            self.dispatch_frame(destination_id, native_frame)
        self.sequence += 1
        command = make_command(grant, destination_id=destination_id, sequence=self.sequence,
                               now_ns=now, payload=payload,
                               target_position_m=target_position_m, target_frame=target_frame)
        if not self.send("command", command, grant.leader_id):
            raise BlockingIOError("native UDP send queue full")
        self.emit("gcs_send", command=command)
        return command

    def operator(self, i: int):
        raw, source = self.clients[i].recvfrom(65535)
        self.client_peers[i] = source
        for message in self.parsers[i].parse_buffer(raw) or []:
            try:
                if message.get_type() == "BAD_DATA":
                    raise ProtocolError("invalid_MAVLink_frame")
                if getattr(message, "target_system", i) not in (0, i):
                    raise ProtocolError("MAVLink_target_mismatch")
                self.dispatch_frame(i, bytes(message.get_msgbuf()))
            except (ProtocolError, BlockingIOError) as exc:
                self.emit("operator_data_rejected", destination_id=i, reason=str(exc))

    def telemetry_eligible(self, grant: Grant, producer_ns: int, now: int) -> bool:
        return (self.issued_grants.get(grant.grant_sequence) == grant
                and type(producer_ns) is int
                and grant.valid_from_ns <= producer_ns <= now < grant.valid_until_ns
                and now - producer_ns <= self.args.telemetry_max_age_ms * 1e6)

    def prepare_telemetry_scope(self, i: int, grant: Grant, producer_ns: int, sequence: int):
        scope = self.telemetry_scope.get(i)
        if (scope is None or scope[0] != (grant.epoch, grant.leader_id)
                or producer_ns >= scope[1]):
            # Public BSF1 sequence anchors a restarted stream; no 500-record wait.
            self.receivers[i] = Reassembler(channel="control", uav_id=i, direction="uart_to_gcs",
                    initial_sequence=sequence, max_age_ms=self.args.telemetry_max_age_ms)
            self.telemetry_provenance[i].clear()
            self.stream_provenance[i].clear()
            self.uart_parsers[i] = self.mavutil.mavlink.MAVLink(None)
            self.uart_parsers[i].robust_parsing = True
            self.latest_messages[i].clear()
            self.latest_message_ns[i].clear()
            self.latest_message_received_ns[i].clear()
            self.emit("telemetry_scope_started", uav_id=i, epoch=grant.epoch,
                      leader_id=grant.leader_id, grant_sequence=grant.grant_sequence)
            self.telemetry_scope[i] = ((grant.epoch, grant.leader_id), grant.valid_until_ns)
        else:
            self.telemetry_scope[i] = (scope[0], max(scope[1], grant.valid_until_ns))

    def release_telemetry(self, i: int, records: list[bytes], sent_times: list[int]):
        if len(records) != len(sent_times):
            raise RuntimeError("BSF1 release provenance mismatch")
        for record, producer in zip(records, sent_times):
            grant = self.telemetry_provenance[i].pop(producer, None)
            if grant is None or not self.telemetry_eligible(grant, producer, time.monotonic_ns()):
                self.emit("telemetry_record_rejected", uav_id=i, producer_ns=producer,
                          reason="expired_original_grant_or_telemetry_age")
                continue
            self.deliver(i, record, producer_ns=producer, grant=grant)

    def consume_provenance(self, i: int, count: int):
        parts = []
        while count:
            data, producer, grant = self.stream_provenance[i].popleft()
            consumed = min(count, len(data))
            parts.append((producer, grant))
            if consumed < len(data):
                self.stream_provenance[i].appendleft((data[consumed:], producer, grant))
            count -= consumed
        return parts

    def deliver(self, i: int, record: bytes, *, producer_ns: int, grant: Grant):
        now = time.monotonic_ns()
        if not self.telemetry_eligible(grant, producer_ns, now):
            self.emit("telemetry_record_rejected", uav_id=i, reason="expired_before_MAVLink_parse")
            return
        self.stream_provenance[i].append((record, producer_ns, grant))
        messages = self.uart_parsers[i].parse_buffer(record) or []
        for message in messages:
            if message.get_type() != "BAD_DATA" and int(message.get_srcSystem()) != i:
                self.emit("telemetry_source_rejected", uav_id=i,
                          observed_system_id=int(message.get_srcSystem()))
                raise ProtocolError("MAVLink SYSID disagrees with native UART origin")
            native_frame = bytes(message.get_msgbuf())
            pending = b"".join(part[0] for part in self.stream_provenance[i])
            offset = pending.find(native_frame)
            if not native_frame or offset < 0:
                raise RuntimeError("MAVLink bytes lost their UART production provenance")
            self.consume_provenance(i, offset)
            parts = self.consume_provenance(i, len(native_frame))
            if message.get_type() == "BAD_DATA":
                continue
            now = time.monotonic_ns()
            if not all(self.telemetry_eligible(g, stamp, now) for stamp, g in parts):
                self.emit("telemetry_message_rejected", uav_id=i, reason="expired_frame_prefix")
                continue
            produced = min(stamp for stamp, _ in parts)
            if i in self.client_peers:
                self.clients[i].sendto(native_frame, self.client_peers[i])
            self.latest_messages[i][message.get_type()] = message
            self.latest_message_ns[i][message.get_type()] = produced
            self.latest_message_received_ns[i][message.get_type()] = now
            if message.get_type() == "COMMAND_ACK":
                self.emit("autopilot_ack", uav_id=i, command_id=None,
                          mavlink_command=int(message.command), result=int(message.result),
                          producer_host_monotonic_ns=produced, received_host_monotonic_ns=now,
                          reason="MAVLink_ACK_has_no_application_command_id")
            elif message.get_type() == "STATUSTEXT":
                self.emit("autopilot_status_text", uav_id=i, severity=int(message.severity),
                          text=str(message.text), producer_host_monotonic_ns=produced,
                          received_host_monotonic_ns=now)
            elif message.get_type() == "HEARTBEAT":
                self.emit("autopilot_heartbeat_received", uav_id=i,
                          custom_mode=int(message.custom_mode), base_mode=int(message.base_mode),
                          producer_host_monotonic_ns=produced, received_host_monotonic_ns=now)
        if sum(len(part[0]) for part in self.stream_provenance[i]) > 8192:
            raise RuntimeError("MAVLink parser retained an unbounded UART prefix")

    def handle(self, packet):
        if packet is None:
            return
        kind, sender, body = packet
        if kind == "heartbeat":
            now = time.monotonic_ns()
            peers = body.get("peer_echo_ns")
            if (sender not in UAV_IDS or type(body.get("sent_ns")) is not int
                    or type(body.get("echo_ns")) is not int or not 0 <= body["echo_ns"] <= now
                    or not 0 <= body["sent_ns"] <= now or not isinstance(peers, dict)
                    or any(k not in {str(i) for i in (0, *UAV_IDS)} or type(v) is not int
                           or not 0 <= v <= now for k, v in peers.items())):
                raise ProtocolError("invalid UAV heartbeat")
            self.heartbeat[sender] = (time.monotonic_ns(), body)
            self.emit("gcs_probe_echo_received", uav_id=sender,
                      probe_sent_host_monotonic_ns=body["echo_ns"],
                      reply_sent_host_monotonic_ns=body["sent_ns"],
                      peer_echo_ns=peers)
        elif kind == "telemetry":
            now = time.monotonic_ns()
            grant = self.issued_grants.get(body.get("grant_sequence"))
            producer = body.get("producer_ns")
            i = body.get("origin_id")
            if (grant is None or sender != grant.leader_id or type(i) is not int or i not in UAV_IDS
                    or body.get("leader_id") != sender
                    or body.get("epoch") != grant.epoch
                    or not self.telemetry_eligible(grant, producer, now)):
                raise ProtocolError("telemetry did not return through selected coordinator")
            payload = base64.b64decode(body["payload"], validate=True)
            chunk = decode_chunk(payload)
            if chunk.sent_monotonic_ns != producer or chunk.uav_id != i:
                raise ProtocolError("outer telemetry provenance disagrees with BSF1 header")
            self.prepare_telemetry_scope(i, grant, producer, chunk.sequence)
            previous = self.telemetry_provenance[i].get(producer)
            if previous is not None and previous != grant:
                raise ProtocolError("one UART record changed its producing grant")
            self.telemetry_provenance[i][producer] = grant
            self.emit("gcs_receive", uav_id=i, origin_id=i, leader_id=sender, epoch=grant.epoch,
                      payload_sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
            receiver = self.receivers[i]
            records = receiver.ingest(payload, time.monotonic_ns())
            self.release_telemetry(i, records, receiver.released_sent_ns)
        else:
            raise ProtocolError("unexpected GCS input kind")

    def step(self):
        self.guard_radio()
        now = time.monotonic_ns()
        self.periodic(now)
        self.update_role(now)
        for key, _ in self.selector.select(.01):
            if key.data == "radio":
                try:
                    self.handle(self.receive())
                except (ProtocolError, ValueError) as exc:
                    self.emit("packet_rejected", reason=str(exc))
            else:
                self.operator(key.data)
        for i, receiver in self.receivers.items():
            records = receiver.expire(time.monotonic_ns())
            self.release_telemetry(i, records, receiver.released_sent_ns)
            now = time.monotonic_ns()
            self.telemetry_provenance[i] = {stamp: grant for stamp, grant in self.telemetry_provenance[i].items()
                                            if self.telemetry_eligible(grant, stamp, now)}

    def close(self):
        for client in self.clients.values():
            client.close()
        super().close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("gcs", "uav"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=15000)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--radio-watchdog-file", type=Path)
    parser.add_argument("--watchdog-s", type=float, default=.3)
    parser.add_argument("--heartbeat-s", type=float, default=.2)
    parser.add_argument("--duration-s", type=float, default=600)
    parser.add_argument("--uav-id", type=int, choices=UAV_IDS)
    parser.add_argument("--tty")
    parser.add_argument("--policy", choices=("fixed", "failover"), default="failover")
    parser.add_argument("--initial-master", type=int, choices=UAV_IDS, default=1)
    parser.add_argument("--lease-s", type=float, default=2)
    parser.add_argument("--renew-s", type=float, default=.5)
    parser.add_argument("--detection-s", type=float, default=1)
    parser.add_argument("--skew-margin-s", type=float, default=.01)
    parser.add_argument("--telemetry-max-age-ms", type=float, default=250.)
    parser.add_argument("--command-max-age-ms", type=float, default=250.)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if os.environ.get("BAS_SIMULATION_MODE", "realtime") != "realtime":
        parser.error("coordination service currently requires realtime host-monotonic mode")
    if args.role == "uav" and (args.uav_id is None or not args.tty):
        parser.error("uav requires --uav-id and --tty")
    if not 0 < args.heartbeat_s < args.detection_s < args.lease_s or not 0 < args.renew_s < args.lease_s:
        parser.error("require heartbeat < detection < lease and 0 < renew < lease")
    if (not 1 <= args.port <= 65535 or not args.duration_s > 0 or not args.watchdog_s > 0
            or args.skew_margin_s < 0 or not 0 < args.telemetry_max_age_ms <= 250):
        parser.error("invalid port/duration/watchdog/skew margin")
    if not 0 < args.command_max_age_ms <= 250:
        parser.error("control command max age must preserve canonical 250 ms deadline")
    emit = Events(args.event_log, args.run_id, args.uav_id if args.role == "uav" else 0)
    service = None
    try:
        service = GCSService(args, emit) if args.role == "gcs" else UAVService(args, emit)
        if args.ready_file:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.ready_file.write_text(json.dumps(dict(pid=os.getpid(), clock_domain="host_monotonic")) + "\n")
        emit("service_started", role=args.role)
        deadline = time.monotonic() + args.duration_s
        while service.running and time.monotonic() < deadline:
            service.guard_radio()
            service.step()
    except Exception as exc:
        emit("service_failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if service:
            service.close()
        emit("service_stopped")
        emit.stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
