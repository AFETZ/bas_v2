#!/usr/bin/env python3
"""Serial/UDP/TCP endpoint for the existing BSF1 native radio boundary."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import time
try:
    from .serial_transport import HEADER, Encoder, Reassembler, TransportCounters, decode_chunk, BoundedQueue, radio_is_live
except ImportError:  # direct script entrypoint
    from serial_transport import HEADER, Encoder, Reassembler, TransportCounters, decode_chunk, BoundedQueue, radio_is_live


IO_BATCH = 64
TOS_BY_CHANNEL = {"control": 184, "payload": 40}  # Same native UART boundary as communication_vertical.


def stream_read_capacity(queue, max_payload, maximum=4096):
    """Read only bytes whose complete BSF1 encoding fits the existing queue."""
    slots = queue.limit - len(queue.items)
    available = queue.byte_limit - queue.bytes
    full, remainder = divmod(available, HEADER.size + max_payload)
    payload = full * max_payload + max(0, remainder - HEADER.size)
    return max(0, min(maximum, slots * max_payload, payload))


def serial_baud_readback(device):
    """Read the opened UART's OS speeds; pySerial.baudrate is only a request."""
    result = dict(serial_baud_observed=None,
                  serial_baud_observed_reason='unsupported_os_termios_unavailable',
                  serial_termios=None, serial_baud_readback_host_monotonic_ns=None)
    if os.name != 'posix':
        return result
    import termios
    try:
        attributes = termios.tcgetattr(device.fileno())
    except (OSError, ValueError, termios.error) as error:
        result['serial_baud_observed_reason'] = 'termios_readback_error:' + type(error).__name__
        return result
    incoming, outgoing = attributes[4:6]
    result.update(serial_termios=dict(input_speed=incoming, output_speed=outgoing, cflag=attributes[2]),
                  serial_baud_readback_host_monotonic_ns=time.monotonic_ns())
    if incoming != outgoing:
        result['serial_baud_observed_reason'] = 'termios_input_output_speed_mismatch'
        return result
    # BOTHER/custom speeds and aliases are not evidence of a numeric baud rate.
    rates = {getattr(termios, name): int(name[1:]) for name in dir(termios)
             if name.startswith('B') and name[1:].isdecimal() and int(name[1:]) > 0}
    rate = rates.get(incoming)
    result.update(serial_baud_observed=rate, serial_baud_observed_reason=(
        'standard_termios_input_output_match' if rate is not None else 'custom_zero_or_unrecognized_termios_speed'))
    return result


def open_radio_socket(config):
    """Bind only the modeled socket in its namespace; physical I/O stays on host."""
    radio = config['radio']
    tos = radio.get('tos', TOS_BY_CHANNEL[config['channel']])
    if type(tos) is not int or not 0 <= tos <= 255:
        raise ValueError('radio.tos must be an integer in 0..255')
    namespace = config.get('radio_namespace')
    saved = target = udp = None
    entered = False
    libc = None
    try:
        if namespace:
            saved = os.open('/proc/self/ns/net', os.O_RDONLY)
            target = os.open('/var/run/netns/' + namespace, os.O_RDONLY)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.setns(target, 0) != 0:
                raise OSError(ctypes.get_errno(), 'cannot enter radio namespace')
            entered = True
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
        if udp.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != tos:
            raise RuntimeError('radio socket did not retain requested TOS')
        udp.bind(tuple(radio['bind']))
        udp.setblocking(False)
        return udp
    except BaseException:
        if udp is not None:
            udp.close()
        raise
    finally:
        restored = not entered or libc.setns(saved, 0) == 0
        for descriptor in (saved, target):
            if descriptor is not None:
                os.close(descriptor)
        if not restored:
            if udp is not None:
                udp.close()
            raise OSError(ctypes.get_errno(), 'cannot restore endpoint namespace')


def flush_queue(queue, write, *, datagram=False, limit=IO_BATCH):
    """Drain a bounded turn; keep stream suffixes and their original deadline.

    Socket/serial writers return accepted bytes, not physical delivery. Datagram
    sends are atomic: a short send is an error, never a second shortened datagram.
    A zero serial write or EWOULDBLOCK is backpressure and retains the queue.
    """
    sent = operations = 0
    maximum_delay_ms = 0.0
    for _ in range(limit):
        now = time.monotonic()
        queue.expire(now)
        if not queue.items:
            break
        data, queued = queue.items[0]
        try:
            count = write(data)
        except BlockingIOError:
            break
        if datagram and count != len(data):
            raise OSError("short UDP datagram write")
        if not count:
            break
        queue.sent(count)
        sent += count
        operations += 1
        maximum_delay_ms = max(maximum_delay_ms, (now - queued) * 1000)
    return sent, operations, maximum_delay_ms


def run(config, output, duration=None):
    if config['queues']['bytes'] <= HEADER.size:
        raise ValueError('queues.bytes must fit a BSF1 header and at least one payload byte')
    endpoint = config["endpoint"]
    kind = endpoint["kind"]
    if kind not in ("serial", "udp", "tcp_client"):
        raise ValueError("endpoint.kind must be serial, udp or tcp_client")
    if kind == "serial":
        import serial
    radio = config["radio"]
    ns = config.get("namespace")
    if ns and os.stat("/proc/self/ns/net").st_ino != os.stat("/var/run/netns/"+ns).st_ino:
        raise ValueError(f"run this adapter with ip netns exec {ns}")
    peer = tuple(radio["peer"])
    # Socket namespace is fixed at creation. Keep the physical endpoint in the
    # host network namespace, and only the radio socket inside its modeled TAP.
    udp = open_radio_socket(config)
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
                   radio_tx_datagrams=0, connection_epoch_drops=0,
                   endpoint_record_drops=0,
                   radio_tos=udp.getsockopt(socket.IPPROTO_IP, socket.IP_TOS), pid=os.getpid(),
                   uav_id=int(config['uav_id']), channel=config['channel'],
                   controller_kind=kind, hardware_validation="not_performed",
                   serial_baud_requested=endpoint['baud'] if kind == 'serial' else None,
                   serial_baud_reported_by_pyserial=None, serial_baud_observed=None,
                   serial_baud_observed_reason='not_connected' if kind == 'serial' else None,
                   serial_termios=None, serial_baud_readback_host_monotonic_ns=None,
                   serial_exclusive=None, serial_device_identity=None,
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
    def publish(now, radio_live):
        metrics.update(sampled_monotonic_ns=time.monotonic_ns(), connected=device is not None, radio_live=radio_live,
            endpoint_silent=now-last_device_rx > watchdog, radio_silent=now-last_radio_rx > watchdog,
            to_radio_queue_bytes=to_radio.bytes, to_device_queue_bytes=to_device.bytes,
            reassembly_pending_records=reassembler.pending_records,
            queue_drops=to_radio.dropped+to_device.dropped,
            deadline_drops=to_radio.expired+to_device.expired,
            peak_queue_bytes=max(to_radio.peak,to_device.peak), transport=counters.as_dict())
        temporary = output/"metrics.tmp"
        temporary.write_text(json.dumps(metrics, indent=2)+"\n")
        temporary.replace(output/"metrics.json")
        log.flush()
    def disconnect(reason):
        nonlocal device, reassembler
        if device is not None:
            device.close()
        device = None
        to_device.clear()
        to_radio.clear()
        reassembler = decoder()
        event("disconnected", reason=reason)
    accept_after_ns = time.monotonic_ns()
    was_radio_live = False
    radio_live = False
    try:
        while running and (duration is None or time.monotonic()-started < duration):
            now = time.monotonic()
            progress = False
            if device is None and now >= retry_at:
                retry_at = now+reconnect
                try:
                    if kind == "serial":
                        device = serial.Serial(endpoint["device"], baudrate=endpoint["baud"],
                            bytesize=endpoint["bytesize"], parity=endpoint["parity"],
                            stopbits=endpoint["stopbits"], timeout=0, write_timeout=0,
                            **({'exclusive': True} if os.name == 'posix' else {}),
                            **{key: endpoint[key] for key in ("xonxoff", "rtscts", "dsrdtr") if key in endpoint})
                        metrics['serial_baud_reported_by_pyserial'] = device.baudrate
                        metrics.update(serial_baud_readback(device))
                        if os.name == 'posix' and metrics['serial_baud_observed'] != endpoint['baud']:
                            raise OSError(f"UART baud mismatch: requested {endpoint['baud']}, "
                                          f"observed {metrics['serial_baud_observed']} "
                                          f"({metrics['serial_baud_observed_reason']})")
                        if os.name == 'posix':
                            # pySerial's cooperative flock protects these BAS
                            # owners; it is not a kernel ban on unrelated readers.
                            metrics['serial_exclusive'] = device.exclusive is True
                            metrics['serial_device_identity'] = os.fstat(device.fileno()).st_rdev
                    elif kind == "udp":
                        device = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        device.bind(tuple(endpoint["bind"]))
                        device.setblocking(False)
                    else:
                        device = socket.create_connection(tuple(endpoint["peer"]), timeout=min(reconnect, .2))
                        device.setblocking(False)
                    metrics["reconnects"] += 1
                    accept_after_ns = time.monotonic_ns()
                    reassembler = decoder()
                    event("connected", kind=kind)
                except OSError as exc:
                    disconnect(str(exc))
            for queue in (to_radio, to_device):
                queue.expire(now)
            # Optional heartbeat is written by the live native runtime. Stale
            # radio state prevents queued commands from being replayed on recovery.
            state_file = config.get("radio_watchdog_file")
            radio_live = radio_is_live(state_file, watchdog)
            if radio_live and not was_radio_live:
                accept_after_ns = time.monotonic_ns()
            if not radio_live:
                to_radio.clear()
                to_device.clear()
                reassembler = decoder()
            was_radio_live = radio_live
            try:
                if device is not None:
                    try:
                        capacity = stream_read_capacity(to_radio, encoder.max_payload) if radio_live else 4096
                        if kind == "serial":
                            data = device.read(capacity) if capacity else b''
                        elif kind == "udp":
                            data, source = device.recvfrom(65535)
                            if source != tuple(endpoint["peer"]):
                                metrics["unexpected_peer"] += 1
                                data = b""
                        else:
                            data = device.recv(capacity) if capacity else b''
                            if capacity and not data:
                                raise ConnectionError("TCP peer closed")
                        if data:
                            progress = True
                            last_device_rx = now
                            metrics["endpoint_to_radio_bytes"] += len(data)
                            if radio_live:
                                frames = encoder.encode(data)
                                # UDP cannot backpressure its producer; reject a
                                # whole record rather than enqueue orphan fragments.
                                if (len(frames) > to_radio.limit - len(to_radio.items) or
                                        sum(map(len, frames)) > to_radio.byte_limit - to_radio.bytes):
                                    to_radio.dropped += len(frames)
                                    metrics['endpoint_record_drops'] += 1
                                else:
                                    for frame in frames:
                                        to_radio.put(frame, now)
                    except BlockingIOError:
                        pass
                for _ in range(IO_BATCH):
                    try:
                        data, source = udp.recvfrom(65535)
                    except BlockingIOError:
                        break
                    progress = True
                    if source == peer:
                        last_radio_rx = time.monotonic()
                        try:
                            chunk = decode_chunk(data)
                            age = (time.monotonic_ns()-chunk.sent_monotonic_ns)/1e9
                        except ValueError:
                            counters.malformed_chunks += 1
                            continue
                        # Both BSF1 adapters run on this simulation host. An external
                        # controller's own clock is never used for this deadline.
                        if not radio_live or age > policy["deadline_s"] or age < 0:
                            metrics["stale_radio_drops"] += 1
                            continue
                        if chunk.sent_monotonic_ns < accept_after_ns:
                            metrics["connection_epoch_drops"] += 1
                            continue
                        records = reassembler.ingest(data)
                        if device is not None and radio_live:
                            for record, sent_ns in zip(records, reassembler.released_sent_ns):
                                to_device.put(record, sent_ns/1e9)
                    else:
                        metrics["unexpected_peer"] += 1
                records = reassembler.expire()
                for record, sent_ns in zip(records, reassembler.released_sent_ns):
                    if device is not None and radio_live:
                        to_device.put(record, sent_ns/1e9)
                to_device.expire(time.monotonic())
                if to_radio.items and radio_live:
                    _, operations, _ = flush_queue(to_radio, lambda data: udp.sendto(data, peer), datagram=True)
                    metrics["radio_tx_datagrams"] += operations
                    progress = progress or operations > 0
                if device is not None and to_device.items and radio_live:
                    write = (device.write if kind == "serial" else
                             (lambda data: device.sendto(data, tuple(endpoint["peer"]))) if kind == "udp" else device.send)
                    count, operations, delay = flush_queue(to_device, write, datagram=kind == "udp")
                    metrics["radio_to_endpoint_bytes"] += count
                    metrics["max_queue_delay_ms"] = max(metrics["max_queue_delay_ms"], delay)
                    progress = progress or operations > 0
            except OSError as exc:
                metrics["io_errors"] += 1
                disconnect(str(exc))
            if now >= report_at:
                publish(time.monotonic(), radio_live)
                report_at = now+.25
            if not progress:
                time.sleep(.001)
    finally:
        disconnect("shutdown")
        udp.close()
        publish(time.monotonic(), radio_live)
        log.close()
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-s", type=float)
    args = parser.parse_args()
    text = args.config.read_text()
    if args.config.suffix.lower() == ".json":
        config = json.loads(text)
    else:
        import yaml
        config = yaml.safe_load(text)
    run(config, args.output, args.duration_s)


if __name__ == "__main__":
    main()
