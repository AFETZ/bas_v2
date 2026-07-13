#!/usr/bin/env python3
"""Serve a tiny live RSSI/SNR dashboard backed by a CSV trace."""

from __future__ import annotations

import argparse
import csv
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Live Radio</title>
  <style>
    body { margin: 0; font: 14px system-ui, sans-serif; background: #101418; color: #e5e7eb; }
    header { padding: 12px 16px; border-bottom: 1px solid #26313a; display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
    main { padding: 16px; }
    canvas { width: 100%; height: min(66vh, 620px); background: #141b21; border: 1px solid #26313a; }
    .meta { color: #9ca3af; }
    .state { padding: 2px 8px; border-radius: 4px; background: #26313a; color: #e5e7eb; }
    .state.down, .state.critical_only { background: #7f1d1d; }
    .state.degraded, .state.marginal { background: #854d0e; }
    .state.excellent, .state.good, .state.usable { background: #14532d; }
  </style>
</head>
<body>
  <header>
    <strong>Live Radio</strong>
    <span class="meta" id="meta">waiting for samples</span>
    <span class="state" id="state">unknown</span>
  </header>
  <main><canvas id="plot" width="1200" height="620"></canvas></main>
  <script>
    const canvas = document.getElementById('plot');
    const ctx = canvas.getContext('2d');
    const meta = document.getElementById('meta');
    const stateBadge = document.getElementById('state');

    function finite(v) { return Number.isFinite(v); }
    function range(values, fallbackMin, fallbackMax) {
      const xs = values.filter(finite);
      if (!xs.length) return [fallbackMin, fallbackMax];
      let lo = Math.min(...xs), hi = Math.max(...xs);
      if (lo === hi) { lo -= 1; hi += 1; }
      return [lo, hi];
    }
    function draw(data) {
      const w = canvas.width, h = canvas.height;
      const pad = {l: 64, r: 74, t: 28, b: 46};
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = '#141b21';
      ctx.fillRect(0, 0, w, h);
      if (!data.samples.length) return;
      const t = data.samples.map(s => s.time_s);
      const snr = data.samples.map(s => s.snr_db);
      const rssi = data.samples.map(s => s.rssi_dbm);
      const [t0, t1] = range(t, 0, 1);
      const [snr0, snr1] = range(snr, -10, 60);
      const [rssi0, rssi1] = range(rssi, -130, -40);
      const x = v => pad.l + (v - t0) / (t1 - t0) * (w - pad.l - pad.r);
      const yS = v => pad.t + (1 - (v - snr0) / (snr1 - snr0)) * (h - pad.t - pad.b);
      const yR = v => pad.t + (1 - (v - rssi0) / (rssi1 - rssi0)) * (h - pad.t - pad.b);
      ctx.strokeStyle = '#334155';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 5; i++) {
        const yy = pad.t + i / 5 * (h - pad.t - pad.b);
        ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
      }
      function line(vals, mapY, color) {
        ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.beginPath();
        let started = false;
        vals.forEach((v, i) => {
          if (!finite(v)) { started = false; return; }
          if (!started) { ctx.moveTo(x(t[i]), mapY(v)); started = true; }
          else ctx.lineTo(x(t[i]), mapY(v));
        });
        ctx.stroke();
      }
      line(snr, yS, '#2dd4bf');
      line(rssi, yR, '#f87171');
      ctx.fillStyle = '#e5e7eb';
      ctx.fillText('time (s)', w / 2 - 24, h - 14);
      ctx.fillStyle = '#2dd4bf';
      ctx.fillText(`SNR/SINR ${snr0.toFixed(1)}..${snr1.toFixed(1)} dB`, 14, 20);
      ctx.fillStyle = '#f87171';
      ctx.fillText(`RSSI ${rssi0.toFixed(1)}..${rssi1.toFixed(1)} dBm`, w - 220, 20);
      const last = data.samples[data.samples.length - 1];
      stateBadge.textContent = last.link_state || 'unknown';
      stateBadge.className = `state ${last.link_state || 'unknown'}`;
      meta.textContent = `${data.samples.length} samples | t=${last.time_s.toFixed(1)}s | SINR=${last.snr_db} dB | RSSI=${last.rssi_dbm} dBm | tier=${last.service_tier_bps} bps | ${last.source}`;
    }
    async function tick() {
      try {
        const res = await fetch('/data', {cache: 'no-store'});
        draw(await res.json());
      } catch (e) {
        meta.textContent = String(e);
      }
      setTimeout(tick, 1000);
    }
    tick();
  </script>
</body>
</html>
"""


def to_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if parsed == float("inf") or parsed == float("-inf"):
        return None
    return parsed


def read_samples(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows = list(csv.DictReader(path.open("r", encoding="utf-8")))
    samples: list[dict[str, object]] = []
    for row in rows[-limit:]:
        time_s = to_float(row.get("time_s") or row.get("elapsed_s"))
        snr = to_float(row.get("snr_db") or row.get("sinr_db"))
        rssi = to_float(row.get("rssi_dbm") or row.get("rx_power_dbm"))
        if time_s is None:
            continue
        samples.append(
            {
                "time_s": time_s,
                "snr_db": snr,
                "rssi_dbm": rssi,
                "source": row.get("source", ""),
                "link_state": row.get("link_state", ""),
                "service_tier_bps": row.get("service_tier_bps", ""),
                "stale": row.get("stale", ""),
            }
        )
    return samples


def build_handler(csv_path: Path, limit: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/data":
                body = json.dumps({"samples": read_samples(csv_path, limit)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), build_handler(Path(args.csv), args.limit))
    print(f"dashboard listening on http://{args.host}:{args.port}/ csv={args.csv}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
