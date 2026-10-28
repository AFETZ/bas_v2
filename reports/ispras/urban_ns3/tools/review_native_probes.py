#!/usr/bin/env python3
"""Independent read-only component review; never runs ns-3 or a flight.

Reuses the existing tcpdump IPv4/UDP decoder, then checks actual probe sequence
IDs, declared LOS/NLOS loss calculations and equal-parameter negative control.
Arithmetic here validates saved outputs; it is not a runtime propagation model.
"""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from scripts.product.inspect_relay_path import HEX, packet


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def received_sequences(path):
    argv = ["tcpdump", "-nn", "-tt", "-e", "-x", "-s", "0", "-r", str(path),
            "udp", "and", "dst", "host", "10.42.0.2"]
    process = subprocess.run(argv, capture_output=True, text=True, check=True)
    header, words, result = None, [], defaultdict(set)

    def decode():
        decoded = packet(header, words)
        if decoded and decoded["dst_port"] in (9000, 9002):
            payload = decoded["payload"]
            if len(payload) != 64 or payload[4:] != bytes(60):
                raise ValueError("unexpected probe payload")
            result[decoded["dst_port"]].add(int.from_bytes(payload[:4], "big"))

    for line in process.stdout.splitlines():
        match = HEX.match(line.rstrip())
        if match:
            words.append(match[1].replace(" ", ""))
        elif line[:1].isdigit():
            if header is not None:
                decode()
            header, words = line, []
    if header is not None:
        decode()
    return result, argv


def review(directory):
    directory = directory.resolve()
    ledger_path = directory / "commands.json"
    ledger = json.loads(ledger_path.read_text())
    hashes, results = {str(ledger_path): sha(ledger_path)}, []
    for recorded in ledger:
        case = recorded["case"]
        csv_path, pcap_path, packet_path = (directory / f"{case}{suffix}" for suffix in
                                          ("-geometry.csv", "-1-0.pcap", "-packets.json"))
        config_name = {"moved": "moved", "equal": "equal", "los_only": "los_only"}.get(case, "unchanged")
        config_path = directory / f"{config_name}-config.json"
        for path in (csv_path, pcap_path, packet_path, config_path):
            hashes[str(path)] = sha(path)
        rows = list(csv.DictReader(csv_path.open()))
        direct = next(r for r in rows if (r["from_id"], r["to_id"]) == ("0", "1"))
        config = json.loads(config_path.read_text())
        model = config["radio"]["los" if direct["radio_state"] == "LOS" else "nlos"]
        distance = math.dist([float(direct[f"from_{k}"]) for k in "xyz"],
                             [float(direct[f"to_{k}"]) for k in "xyz"])
        expected = model["reference_loss_db"] + 10*model["exponent"]*math.log10(
            max(1, distance/model["reference_distance_m"]))
        error = float(direct["pathloss_db"]) - expected
        received, decode_argv = received_sequences(pcap_path)
        counts = json.loads(packet_path.read_text())
        if any(not 0 <= seq < counts["offered_each_path"] for values in received.values() for seq in values):
            raise ValueError("received probe sequence outside offered range")
        if abs(error) > 1e-9:
            raise ValueError(f"{case}: saved loss differs from declared model")
        if (len(received[9000]), len(received[9002])) != (
                counts["direct_unique_received"], counts["relay_unique_received_second_hop"]):
            raise ValueError(f"{case}: PCAP sequence IDs disagree with native callback counts")
        results.append(dict(case=case, geom_los=int(direct["geom_los"]), radio_state=direct["radio_state"],
            loss_db=float(direct["pathloss_db"]), formula_error_db=error,
            direct_received_unique=len(received[9000]), via_relay_received_unique=len(received[9002]),
            direct_sequences=sorted(received[9000]), via_relay_sequences=sorted(received[9002]),
            pcap_decode_argv=decode_argv))
    return dict(scope="saved static three-node probes; not live five-UAV or field validation",
        source_raw=str(directory), script_sha256=sha(Path(__file__)),
        decoder_script_sha256=sha(ROOT / "scripts/product/inspect_relay_path.py"),
        tcpdump_version=subprocess.run(["tcpdump", "--version"], capture_output=True, text=True, check=True).stdout,
        input_sha256=hashes, results=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.raw.resolve() or args.raw.resolve() in args.output.resolve().parents:
        parser.error("output must be outside immutable raw")
    value = review(args.raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps([dict(case=r["case"], loss_db=r["loss_db"], direct=r["direct_received_unique"],
                          via_relay=r["via_relay_received_unique"]) for r in value["results"]], indent=2))


if __name__ == "__main__":
    main()
