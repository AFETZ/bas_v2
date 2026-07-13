#!/usr/bin/env python3
"""Plot live radio CSV traces with SNR/SINR and RSSI columns."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def first_present(row: dict[str, str], names: list[str]) -> float | None:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return float(value)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--title", default="Live radio")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    rows = list(csv.DictReader(input_path.open("r", encoding="utf-8")))
    if not rows:
        raise SystemExit(f"no samples in {input_path}")

    x: list[float] = []
    snr: list[float] = []
    rssi: list[float] = []
    for row in rows:
        time_s = first_present(row, ["time_s", "elapsed_s"])
        snr_value = first_present(row, ["snr_db", "sinr_db"])
        rssi_value = first_present(row, ["rssi_dbm", "rx_power_dbm"])
        if time_s is None:
            continue
        x.append(time_s)
        snr.append(snr_value if snr_value is not None else float("nan"))
        rssi.append(rssi_value if rssi_value is not None else float("nan"))

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax_snr = plt.subplots(figsize=(10, 5.2))
    ax_snr.plot(x, snr, color="#0f766e", linewidth=2.0, label="SNR/SINR (dB)")
    ax_snr.scatter(x[-1:], snr[-1:], color="#0f766e", s=28, zorder=3)
    ax_snr.set_xlabel("time (s)")
    ax_snr.set_ylabel("SNR/SINR (dB)", color="#0f766e")
    ax_snr.tick_params(axis="y", labelcolor="#0f766e")
    ax_snr.grid(True, color="#d1d5db", linewidth=0.7)

    ax_rssi = ax_snr.twinx()
    ax_rssi.plot(x, rssi, color="#b91c1c", linewidth=1.8, label="RSSI (dBm)")
    ax_rssi.scatter(x[-1:], rssi[-1:], color="#b91c1c", s=28, zorder=3)
    ax_rssi.set_ylabel("RSSI (dBm)", color="#b91c1c")
    ax_rssi.tick_params(axis="y", labelcolor="#b91c1c")

    fig.suptitle(args.title)
    fig.tight_layout()
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    fig.savefig(temp, dpi=140, format="png")
    plt.close(fig)
    temp.replace(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
