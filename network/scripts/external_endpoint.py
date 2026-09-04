#!/usr/bin/env python3
"""Serial/UDP/TCP endpoint for the existing BSF1 native radio boundary."""
from __future__ import annotations
import argparse
from collections import deque
import json
import os
from pathlib import Path
import signal
import socket
import time
import yaml
from serial_transport import Encoder, Reassembler, TransportCounters, decode_chunk


class BoundedQueue:
    def __init__(self, packets, byte_limit, deadline_s):
        if packets <= 0 or byte_limit <= 0 or deadline_s <= 0:
            raise ValueError("queue bounds and deadline must be positive")
        self.items = deque()
        self.limit = packets
        self.byte_limit = byte_limit
        self.deadline_s = deadline_s
        self.bytes = self.dropped = self.expired = self.peak = 0

    def put(self, data, now):
        if len(self.items) >= self.limit or self.bytes+len(data) > self.byte_limit:
            self.dropped += 1
            return False
        self.items.append([bytes(data), now])
        self.bytes += len(data)
        self.peak = max(self.peak, self.bytes)
        return True

    def clear(self):
        self.dropped += len(self.items)
        self.items.clear()
        self.bytes = 0

    def expire(self, now):
        while self.items and now-self.items[0][1] > self.deadline_s:
            self.bytes -= len(self.items.popleft()[0])
            self.expired += 1

    def sent(self, count):
        self.bytes -= count
        data, timestamp = self.items[0]
        if count == len(data):
            self.items.popleft()
        else:
            self.items[0] = [data[count:], timestamp]


def run(config, output, duration=None):
    import serial
    endpoint = config["endpoint"]
    kind = endpoint["kind"]
    if kind not in ("serial", "udp", "tcp_client"):
        raise ValueError("endpoint.kind must be serial, udp or tcp_client")
    radio = config["radio"]
    ns = config.get("namespace")
    if ns and os.stat("/proc/self/ns/net").st_ino != os.stat("/var/run/netns/"+ns).st_ino:
        raise ValueError(f"run this adapter with ip netns exec {ns}")
    peer = tuple(radio["peer"])
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(tuple(radio["bind"]))
    udp.setblocking(False)
    counters = TransportCounters()
    encoder = Encoder(channel=config["channel"], uav_id=int(config["uav_id"]),
                      direction="uart_to_gcs", max_payload=192)
    def decoder():
        return Reassembler(channel=config["channel"], uav_id=int(config["uav_id"]),
                           direction="gcs_to_uart", timeout_ms=max(1,min(250,int(config["queues"]["deadline_s"]*500))), counters=counters,
                           max_buffer_records=config["queues"]["packets"], max_buffer_bytes=config["queues"]["bytes"],
                           max_age_ms=config["queues"]["deadline_s"]*1000)
    reassembler = decoder()
    policy = config["queues"]
    to_radio = BoundedQueue(policy["packets"], policy["bytes"], policy["deadline_s"])
    to_device = BoundedQueue(policy["packets"], policy["bytes"], policy["deadline_s"])
    watchdog = float(config["watchdog_s"])
    reconnect = float(config["reconnect_s"])
    if watchdog <= 0 or reconnect <= 0:
        raise ValueError("watchdog and reconnect must be positive")
    running, device, retry_at = True, None, 0.0
    started = last_device_rx = last_radio_rx = time.monotonic()
    report_at = started
    metrics = dict(endpoint_to_radio_bytes=0, radio_to_endpoint_bytes=0, reconnects=0,
                   io_errors=0, unexpected_peer=0, stale_radio_drops=0, max_queue_delay_ms=0.0,
                   controller_kind=kind, hardware_validation="not_performed",
                   framing="BSF1; unchanged endpoint bytes", clock="host_monotonic")
    output.mkdir(parents=True, exist_ok=True)
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log = (output/"events.jsonl").open("w", buffering=65536)
    def event(name, **values):
        log.write(json.dumps(dict(event=name, monotonic_ns=time.monotonic_ns(), **values))+"\n")
    def disconnect(reason):
        nonlocal device, reassembler
        if device is not None:
            device.close()
        device = None
        to_device.clear()
        to_radio.clear()
        reassembler = decoder()
        event("disconnected", reason=reason)
    try:
        while running and (duration is None or time.monotonic()-started < duration):
            now = time.monotonic()
            if device is None and now >= retry_at:
                retry_at = now+reconnect
                try:
                    if kind == "serial":
                        device = serial.Serial(endpoint["device"], baudrate=endpoint["baud"],
                            bytesize=endpoint["bytesize"], parity=endpoint["parity"],
                            stopbits=endpoint["stopbits"], timeout=0, write_timeout=0)
                    elif kind == "udp":
                        device = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        device.bind(tuple(endpoint["bind"]))
                        device.setblocking(False)
                    else:
                        device = socket.create_connection(tuple(endpoint["peer"]), timeout=min(reconnect, .2))
                        device.setblocking(False)
                    metrics["reconnects"] += 1
                    event("connected", kind=kind)
                except (OSError, serial.SerialException) as exc:
                    disconnect(str(exc))
            for queue in (to_radio, to_device):
                queue.expire(now)
            # Optional heartbeat is written by the live native runtime. Stale
            # radio state prevents queued commands from being replayed on recovery.
            state_file = config.get("radio_watchdog_file")
            radio_live = not state_file or (Path(state_file).is_file() and time.time()-Path(state_file).stat().st_mtime < watchdog)
            if not radio_live:
                to_radio.clear()
                to_device.clear()
                reassembler = decoder()
            try:
                if device is not None:
                    try:
                        if kind == "serial":
                            data = device.read(4096)
                        elif kind == "udp":
                            data, source = device.recvfrom(4096)
                            if source != tuple(endpoint["peer"]):
                                metrics["unexpected_peer"] += 1
                                data = b""
                        else:
                            data = device.recv(4096)
                            if not data:
                                raise ConnectionError("TCP peer closed")
                        if data:
                            last_device_rx = now
                            metrics["endpoint_to_radio_bytes"] += len(data)
                            if radio_live:
                                for frame in encoder.encode(data):
                                    to_radio.put(frame, now)
                    except BlockingIOError:
                        pass
                try:
                    data, source = udp.recvfrom(65535)
                    if source == peer:
                        last_radio_rx = now
                        try:
                            age = (time.monotonic_ns()-decode_chunk(data).sent_monotonic_ns)/1e9
                        except ValueError:
                            counters.malformed_chunks += 1
                            continue
                        # Both BSF1 adapters run on this simulation host. An external
                        # controller's own clock is never used for this deadline.
                        if not radio_live or age > policy["deadline_s"] or age < 0:
                            metrics["stale_radio_drops"] += 1
                            continue
                        records = reassembler.ingest(data)
                        if device is not None and radio_live:
                            for record in records:
                                to_device.put(record, now)
                    else:
                        metrics["unexpected_peer"] += 1
                except BlockingIOError:
                    pass
                for record in reassembler.expire():
                    if device is not None and radio_live:
                        to_device.put(record, now)
                if to_radio.items and radio_live:
                    try:
                        count = udp.sendto(to_radio.items[0][0], peer)
                        to_radio.sent(count)
                    except BlockingIOError:
                        pass
                if device is not None and to_device.items and radio_live:
                    data, queued = to_device.items[0]
                    try:
                        count = (device.write(data) if kind == "serial" else
                                 device.sendto(data, tuple(endpoint["peer"])) if kind == "udp" else device.send(data))
                        metrics["radio_to_endpoint_bytes"] += count
                        metrics["max_queue_delay_ms"] = max(metrics["max_queue_delay_ms"], (now-queued)*1000)
                        to_device.sent(count)
                    except BlockingIOError:
                        pass
            except (OSError, serial.SerialException) as exc:
                metrics["io_errors"] += 1
                disconnect(str(exc))
            if now >= report_at:
                metrics.update(connected=device is not None, radio_live=radio_live,
                    endpoint_silent=now-last_device_rx > watchdog, radio_silent=now-last_radio_rx > watchdog,
                    to_radio_queue_bytes=to_radio.bytes, to_device_queue_bytes=to_device.bytes,
                    queue_drops=to_radio.dropped+to_device.dropped,
                    deadline_drops=to_radio.expired+to_device.expired,
                    peak_queue_bytes=max(to_radio.peak,to_device.peak), transport=counters.as_dict())
                temporary = output/"metrics.tmp"
                temporary.write_text(json.dumps(metrics, indent=2)+"\n")
                temporary.replace(output/"metrics.json")
                log.flush()
                report_at = now+.25
            time.sleep(.001)
    finally:
        disconnect("shutdown")
        udp.close()
        log.close()
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-s", type=float)
    args = parser.parse_args()
    run(yaml.safe_load(args.config.read_text()), args.output, args.duration_s)


if __name__ == "__main__":
    main()
