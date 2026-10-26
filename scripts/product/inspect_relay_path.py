#!/usr/bin/env python3
"""Read TAP PCAPs with tcpdump and correlate native relay hops with real UART logs.

This is an offline packet-path inspection. UART admission does not establish
flight completion; subsequent telemetry is not assigned to a command ACK.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from typing import Iterator

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from network.scripts.serial_transport import FramingError, Reassembler, decode_chunk

MAC = r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}"
ADDRESS = r"\d+\.\d+\.\d+\.\d+"
IP_HEADER = re.compile(rf"({ADDRESS})\.(\d+) > ({ADDRESS})\.(\d+):")
MAC_HEADER = re.compile(rf"({MAC}) > ({MAC}),", re.I)
HEX = re.compile(r"^\s*0x[0-9a-f]+:\s+([0-9a-f ]+)$", re.I)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def records(path: Path, errors: Counter) -> Iterator[dict]:
    if not path.is_file():
        errors[f"missing_log:{path.name}"] += 1
        return
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            yield {**value, "log_file": path.name, "log_line": number}
        except (ValueError, TypeError):
            errors[f"invalid_jsonl:{path.name}"] += 1


def packet(header: str, hex_words: list[str]) -> dict | None:
    """Decode only the IPv4/UDP bytes printed by the existing tcpdump tool."""
    addresses = IP_HEADER.search(header)
    if addresses is None:
        return None
    raw = bytes.fromhex("".join(hex_words))
    if len(raw) < 28 or raw[0] >> 4 != 4:
        raise ValueError("unsupported or truncated IPv4 header")
    offset = (raw[0] & 15) * 4
    if raw[9] != 17:
        return None
    if offset < 20 or len(raw) < offset + 8:
        raise ValueError("truncated UDP packet")
    if struct.unpack_from("!H", raw, 6)[0] & 0x3FFF:
        raise ValueError("fragmented IP datagram")
    udp_length = struct.unpack_from("!H", raw, offset + 4)[0]
    if udp_length < 8 or len(raw) < offset + udp_length:
        raise ValueError("truncated UDP payload")
    mac = MAC_HEADER.search(header)
    return dict(pcap_unix_s=header.split()[0], src_ip=addresses[1],
                src_port=int(addresses[2]), dst_ip=addresses[3], dst_port=int(addresses[4]),
                src_mac=mac[1] if mac else None, dst_mac=mac[2] if mac else None,
                payload=raw[offset + 8:offset + udp_length])


def captured_udp(path: Path, port: int, errors: Counter) -> Iterator[dict]:
    if not path.is_file():
        errors[f"missing_pcap:{path.name}"] += 1
        return
    process = subprocess.Popen(["tcpdump", "-nn", "-tt", "-e", "-x", "-s", "0",
                                "-r", str(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    header, words, number = None, [], 0
    try:
        for line in process.stdout:
            match = HEX.match(line.rstrip())
            if match:
                words.append(match[1].replace(" ", ""))
                continue
            if not line[:1].isspace() and line[:1].isdigit():
                if header is not None:
                    number += 1
                    try:
                        value = packet(header, words)
                        if value and value["src_port"] == port and value["dst_port"] == port:
                            yield {**value, "capture": path.name, "packet_index": number}
                    except ValueError:
                        errors[f"unsupported_or_truncated_packet:{path.name}"] += 1
                header, words = line.rstrip(), []
        if header is not None:
            number += 1
            try:
                value = packet(header, words)
                if value and value["src_port"] == port and value["dst_port"] == port:
                    yield {**value, "capture": path.name, "packet_index": number}
            except ValueError:
                errors[f"unsupported_or_truncated_packet:{path.name}"] += 1
        stderr = process.stderr.read()
        if process.wait() != 0:
            raise ValueError(f"tcpdump failed for {path}: {stderr.strip()}")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
        process.stdout.close()
        process.stderr.close()


def cid(body: dict) -> str:
    return f"{body['epoch']}:{body['destination_id']}:{body['sequence']}"


def endpoint(index: int) -> str:
    return f"10.71.{index}.10"


def on_hop(observation: dict, source: int, destination: int) -> bool:
    return (observation["src_ip"] == endpoint(source)
            and observation["dst_ip"] == endpoint(destination)
            and observation["sender_id"] == source)


def inspect(run: Path, *, command_id=None, epoch=None, port=15000, telemetry_wait_s=2) -> dict:
    errors = Counter()
    events = [row for name in ("gcs", *(f"uav{i}" for i in range(1, 6)))
              for row in records(run / f"logs/coordination_{name}.jsonl", errors)]
    sends = {r["command_id"]: r for r in events if r.get("event") == "gcs_send"}
    admissions = {r["command_id"]: r for r in events if r.get("event") == "uart_admitted"}
    eligible = [r for key, r in sends.items() if key in admissions
                and r.get("destination_id") != r.get("leader_id")]
    if command_id:
        if command_id not in sends or command_id not in admissions:
            raise ValueError("requested command has no GCS send and actual UART admission")
        selected = [sends[command_id]]
        if selected[0]["destination_id"] == selected[0]["leader_id"]:
            raise ValueError("coordinator-local command has no second radio hop; choose a follower command")
        if epoch is not None and epoch != selected[0]["epoch"]:
            raise ValueError("command epoch disagrees with requested epoch")
        epoch = selected[0]["epoch"]
    else:
        if epoch is None:
            epoch = max((r["epoch"] for r in eligible), default=None)
        scope = [r for r in eligible if r["epoch"] == epoch]
        selected = []
        for destination in sorted({r["destination_id"] for r in scope}):
            choices = [r for r in scope if r["destination_id"] == destination]
            choices.sort(key=lambda r: (r.get("target_position_m") is None,
                                       r["host_monotonic_ns"]))
            selected.append(choices[0])
    if not selected:
        raise ValueError("no admitted follower command in the requested epoch")
    run_ids = {r["run_id"] for r in selected}
    if len(run_ids) != 1:
        raise ValueError("selected commands belong to different runs")
    run_id = next(iter(run_ids))
    wanted = {r["command_id"] for r in selected}
    command_packets = defaultdict(list)
    telemetry = defaultdict(lambda: defaultdict(list))
    for name in ("gcs", *(f"uav{i}" for i in range(1, 6))):
        capture = run / f"pcap/tap_{name}.pcap"
        for observed in captured_udp(capture, port, errors):
            try:
                value = json.loads(observed["payload"])
                kind, sender, body = value["kind"], value["sender_id"], value["body"]
                if body.get("run_id") != run_id or kind not in ("command", "telemetry"):
                    continue
                wire = base64.b64decode(body["payload"], validate=True)
                item = {k: v for k, v in observed.items() if k != "payload"}
                item.update(sender_id=sender, udp_payload_sha256=sha(observed["payload"]),
                            payload_sha256=sha(wire), payload_bytes=len(wire))
                if kind == "command" and cid(body) in wanted:
                    item["payload_hex"] = wire.hex()
                    command_packets[cid(body)].append(item)
                elif kind == "telemetry":
                    producer = body["producer_ns"]
                    match = any(body["origin_id"] == r["destination_id"]
                                and body["epoch"] == r["epoch"]
                                and body["leader_id"] == r["leader_id"]
                                and admissions[r["command_id"]]["host_monotonic_ns"] <= producer
                                <= admissions[r["command_id"]]["host_monotonic_ns"] + telemetry_wait_s * 1e9
                                for r in selected)
                    if not match:
                        continue
                    chunk = decode_chunk(wire)
                    if chunk.uav_id != body["origin_id"] or chunk.sent_monotonic_ns != producer:
                        raise ValueError("telemetry producer/header mismatch")
                    key = (body["epoch"], body["leader_id"], body["origin_id"], producer,
                           body.get("grant_sequence"), chunk.sequence)
                    item.update(wire=wire, fragment_index=chunk.fragment_index,
                                fragment_count=chunk.fragment_count)
                    telemetry[key][chunk.fragment_index].append(item)
            except (ValueError, KeyError, TypeError, FramingError):
                errors[f"invalid_relay_packet:{capture.name}"] += 1
    result = []
    gcs_return_hashes = {(r.get("origin_id", r.get("uav_id")), r.get("payload_sha256"))
                         for r in events if r.get("event") == "gcs_receive"}
    uart_raw_hashes = {(r.get("node_id"), r.get("payload_sha256")) for r in events
                       if r.get("event") == "uart_observed"}
    for sent in selected:
        key, master, destination = sent["command_id"], sent["leader_id"], sent["destination_id"]
        admitted = admissions[key]
        observations = command_packets[key]
        hops = []
        for label, source, target in [("GCS→master", 0, master),
                                      ("master→follower", master, destination)]:
            captures = [r for r in observations if on_hop(r, source, target)]
            hops.append(dict(stage=label, observations=captures,
                             payload_matches_uart_log=bool(captures) and all(
                                 r["payload_sha256"] == admitted.get("payload_sha256")
                                 and r["payload_bytes"] == admitted.get("bytes") for r in captures)))
        returned = None
        for tk, fragments in sorted(telemetry.items()):
            if tk[:3] != (sent["epoch"], master, destination):
                continue
            if not admitted["host_monotonic_ns"] <= tk[3] <= admitted["host_monotonic_ns"] + telemetry_wait_s * 1e9:
                continue
            first = next(iter(fragments.values()))[0]
            if set(fragments) != set(range(first["fragment_count"])):
                continue
            reassembler = Reassembler(channel="control", uav_id=destination,
                                      direction="uart_to_gcs", initial_sequence=tk[5])
            assembled, returns, complete = [], [], True
            for index in sorted(fragments):
                values = fragments[index]
                down = [r for r in values if on_hop(r, destination, master)]
                up = [r for r in values if on_hop(r, master, 0)]
                if not down or not up or down[0]["payload_sha256"] != up[0]["payload_sha256"]:
                    complete = False
                    break
                if (destination, up[0]["payload_sha256"]) not in gcs_return_hashes:
                    complete = False
                    break
                assembled.extend(reassembler.ingest(up[0]["wire"], now_ns=tk[3]))
                returns.append(dict(fragment_index=index,
                                    follower_to_master=[{k: v for k, v in r.items() if k != "wire"} for r in down],
                                    master_to_gcs=[{k: v for k, v in r.items() if k != "wire"} for r in up]))
            if complete and len(assembled) == 1 and (destination, sha(assembled[0])) in uart_raw_hashes:
                returned = dict(producer_host_monotonic_ns=tk[3],
                                uart_record_sha256=sha(assembled[0]), uart_record_bytes=len(assembled[0]),
                                fragments=returns,
                                interpretation="Subsequent actual UART telemetry; no command-specific ACK attribution")
                break
        result.append(dict(command_id=key, epoch=sent["epoch"], leader_id=master,
                           destination_id=destination, target_position_m=sent.get("target_position_m"),
                           gcs_send=sent, command_hops=hops, uart_admission=admitted,
                           subsequent_telemetry_return=returned))
    expected = sorted(set(range(1, 6)) - {selected[0]["leader_id"]})
    complete_destinations = [r["destination_id"] for r in result
                             if all(h["payload_matches_uart_log"] for h in r["command_hops"])
                             and r["subsequent_telemetry_return"] is not None]
    initial_grants = sorted((r for r in events if r.get("event") == "grant_issued"),
                            key=lambda r: r["host_monotonic_ns"])
    initial_leader = initial_grants[0]["leader_id"] if initial_grants else None
    return dict(run_id=run_id, epoch=epoch, leader_id=selected[0]["leader_id"],
                initial_leader_id=initial_leader,
                expected_followers=expected, inspected_destinations=sorted(complete_destinations),
                target_bearing_commands=sum(r["target_position_m"] is not None for r in result),
                commands=result, parse_notes=dict(errors), limits=[
                    "TAP captures observe packets; application delivery is supported separately by UART admission logs.",
                    "PCAP timestamps are Unix time. Event/producer timestamps are host monotonic; no cross-clock latency is computed.",
                    "Subsequent UART telemetry is not attributed to a specific command ACK or completed maneuver.",
                    "Absent matches may reflect packet loss, capture gaps or insufficient telemetry window.",
                    "Automatic selection prefers admitted target-bearing commands and labels diagnostic non-target fallback explicitly."])


def render(value: dict) -> str:
    lines = [f"Run `{value['run_id']}` — epoch {value['epoch']}, master UAV{value['leader_id']}",
             f"Observed complete packet/UART/telemetry chains: {value['inspected_destinations']}; expected followers {value['expected_followers']}.",
             f"Target-bearing inspected commands: {value['target_bearing_commands']}.", "",
             "| Command | Stage | Capture / log | Source → destination | MAC source → destination | Bytes | Payload SHA-256 prefix | Time |",
             "|---|---|---|---|---|---:|---|---|"]
    if value['target_bearing_commands'] == 0:
        lines.insert(3, "Selection: diagnostic non-target frames; target execution is not established.")
    if value['initial_leader_id'] is not None and value['initial_leader_id'] != value['leader_id']:
        former = value['initial_leader_id']
        lines.insert(3, f"Former master UAV{former} in observed follower chains: {former in value['inspected_destinations']}.")
    def row(command, stage, item, *, event=False):
        if event:
            return f"| {command} | {stage} | {item['log_file']}:{item['log_line']} | UART UAV{item.get('node_id')} | — | {item.get('bytes', '—')} | {item.get('payload_sha256', '—')[:16]} | mono {item['host_monotonic_ns']} ns |"
        return f"| {command} | {stage} | {item['capture']} #{item['packet_index']} | {item['src_ip']}:{item['src_port']} → {item['dst_ip']}:{item['dst_port']} | {item['src_mac']} → {item['dst_mac']} | {item['payload_bytes']} | {item['payload_sha256'][:16]} | Unix {item['pcap_unix_s']} s |"
    def endpoints(items):
        if not items:
            return []
        selected = []
        for ip in (items[0]['src_ip'], items[0]['dst_ip']):
            index = int(ip.split('.')[2])
            name = 'tap_gcs.pcap' if index == 0 else f'tap_uav{index}.pcap'
            found = next((r for r in items if r['capture'] == name), None)
            if found is not None:
                selected.append(found)
        return selected or items[:1]
    for command in value['commands']:
        key = command['command_id']
        for hop in command['command_hops']:
            if hop['observations']:
                for item in endpoints(hop['observations']):
                    lines.append(row(key, hop['stage'], item))
            else:
                lines.append(f"| {key} | {hop['stage']} | not observed | — | — | — | — | — |")
            if not hop['payload_matches_uart_log']:
                lines.append(f"| {key} | payload consistency | capture missing or differs from UART log | — | — | — | — | — |")
        lines.append(row(key, 'UART admission', command['uart_admission'], event=True))
        returned = command['subsequent_telemetry_return']
        if returned:
            for fragment in returned['fragments']:
                for name, label in [('follower_to_master', 'telemetry follower→master'), ('master_to_gcs', 'telemetry master→GCS')]:
                    for item in endpoints(fragment[name]):
                        lines.append(row(key, label, item))
        else:
            lines.append(f"| {key} | subsequent telemetry return | not observed | — | — | — | — | — |")
    lines += ["", *[f"- {s}" for s in value['limits']]]
    if value['parse_notes']:
        lines += ["", "Reader notes: " + json.dumps(value['parse_notes'], sort_keys=True)]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--command-id')
    parser.add_argument('--epoch', type=int)
    parser.add_argument('--port', type=int, default=15000)
    parser.add_argument('--telemetry-wait-s', type=float, default=2)
    parser.add_argument('--json', type=Path, help='Optional derived report; raw inputs are never modified')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not math.isfinite(args.telemetry_wait_s) or args.telemetry_wait_s <= 0:
        parser.error('require a valid UDP port and a finite positive telemetry window')
    if not shutil.which('tcpdump'):
        parser.error('existing tcpdump executable is required; no dependency is installed')
    try:
        value = inspect(args.run_dir.resolve(), command_id=args.command_id, epoch=args.epoch,
                        port=args.port, telemetry_wait_s=args.telemetry_wait_s)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    print(render(value), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
