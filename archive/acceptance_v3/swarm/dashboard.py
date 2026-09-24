#!/usr/bin/env python3
"""Small stdlib dashboard for long-running Codex swarm jobs."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")
DEFAULT_WORKERS = ["queue", "foundation", "sionna", "ns3", "bridge", "hitl", "validation"]


def repo_root() -> Path:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
        return Path(out.strip())
    except Exception:
        return Path(__file__).resolve().parents[2]


ROOT = repo_root()
RUNS_ROOT = ROOT / "runs" / "codex-swarm"
SWARM_ROOT = ROOT.parent / "codex-swarm" / ROOT.name


def read_text(path: Path, max_bytes: int | None = None) -> str:
    try:
        if max_bytes is None:
            return path.read_text(errors="replace")
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            return handle.read().decode(errors="replace")
    except FileNotFoundError:
        return ""
    except IsADirectoryError:
        return ""


def tail_lines(path: Path, lines: int = 120, max_bytes: int = 256_000) -> str:
    text = read_text(path, max_bytes=max_bytes)
    return "\n".join(text.splitlines()[-lines:])


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in read_text(path).splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def pid_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"pid": None, "running": False, "status": "unknown"}
    raw = read_text(path).strip()
    try:
        pid = int(raw)
    except ValueError:
        return {"pid": raw, "running": False, "status": "bad pid"}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"pid": pid, "running": False, "status": "stopped"}
    except PermissionError:
        return {"pid": pid, "running": True, "status": "running"}
    return {"pid": pid, "running": True, "status": "running"}


def latest_run_id() -> str:
    marker = ROOT / "network" / "swarm" / ".last_run"
    marked = read_text(marker).strip()
    if marked:
        return marked
    if not RUNS_ROOT.exists():
        return ""
    runs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()]
    if not runs:
        return ""
    return max(runs, key=lambda p: p.stat().st_mtime).name


def list_runs() -> list[dict[str, Any]]:
    if not RUNS_ROOT.exists():
        return []
    runs = []
    for path in RUNS_ROOT.iterdir():
        if not path.is_dir():
            continue
        runs.append(
            {
                "id": path.name,
                "mtime": path.stat().st_mtime,
                "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
            }
        )
    return sorted(runs, key=lambda item: item["mtime"], reverse=True)


def parse_recent_events(path: Path) -> dict[str, Any]:
    text = tail_lines(path, lines=220, max_bytes=512_000)
    last_agent = ""
    last_command = ""
    last_command_status = ""
    last_tool = ""
    completed_turn = False
    command_count = 0
    file_changes = 0

    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        item_type = item.get("type")
        if event.get("type") == "turn.completed":
            completed_turn = True
        if item_type == "agent_message":
            last_agent = item.get("text", "")
        elif item_type == "command_execution":
            last_command = item.get("command", "")
            last_command_status = item.get("status") or str(item.get("exit_code", ""))
            command_count += 1
        elif item_type == "file_change":
            last_tool = "file change"
            file_changes += 1
        elif item_type:
            last_tool = item_type

    return {
        "last_agent": last_agent,
        "last_command": last_command,
        "last_command_status": last_command_status,
        "last_tool": last_tool,
        "turn_completed": completed_turn,
        "command_count_recent": command_count,
        "file_changes_recent": file_changes,
    }


def worker_status(run_id: str, worker: str) -> dict[str, Any]:
    log_dir = RUNS_ROOT / run_id / worker
    worktree = SWARM_ROOT / run_id / "worktrees" / worker
    meta = parse_env(log_dir / "meta.env")
    pid = pid_state(log_dir / "pid")
    exitcode_path = worktree / "runs" / "codex" / f"{worker}.exitcode"
    final_path = worktree / "runs" / "codex" / f"{worker}.final.md"
    events_path = log_dir / "events.jsonl"
    stderr_path = log_dir / "stderr.log"
    queue_path = log_dir / "queue.out"
    source_path = events_path if events_path.exists() else queue_path
    event_info = parse_recent_events(events_path) if events_path.exists() else {}

    changed = ""
    if worktree.exists():
        try:
            changed = subprocess.check_output(
                ["git", "-C", str(worktree), "status", "--short"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            changed = ""

    return {
        "worker": worker,
        "log_dir": str(log_dir),
        "worktree": str(worktree) if worktree.exists() else meta.get("worktree", ""),
        "pid": pid.get("pid"),
        "running": pid.get("running"),
        "status": pid.get("status"),
        "exitcode": read_text(exitcode_path).strip() if exitcode_path.exists() else "",
        "final_bytes": final_path.stat().st_size if final_path.exists() else 0,
        "events_bytes": events_path.stat().st_size if events_path.exists() else 0,
        "stderr_bytes": stderr_path.stat().st_size if stderr_path.exists() else 0,
        "queue_bytes": queue_path.stat().st_size if queue_path.exists() else 0,
        "updated": source_path.stat().st_mtime if source_path.exists() else log_dir.stat().st_mtime if log_dir.exists() else 0,
        "updated_text": time.strftime("%H:%M:%S", time.localtime(source_path.stat().st_mtime)) if source_path.exists() else "",
        "meta": meta,
        "changed": changed,
        "recent": event_info,
    }


def collect_status(run_id: str | None = None) -> dict[str, Any]:
    run_id = run_id or latest_run_id()
    log_root = RUNS_ROOT / run_id if run_id else RUNS_ROOT
    workers: list[str] = []
    if log_root.exists():
        workers = [p.name for p in log_root.iterdir() if p.is_dir()]
    for worker in DEFAULT_WORKERS:
        if worker in workers:
            workers.remove(worker)
            workers.append(worker)
    ordered = [worker for worker in DEFAULT_WORKERS if (log_root / worker).exists()]
    ordered.extend(sorted(worker for worker in workers if worker not in ordered))

    return {
        "root": str(ROOT),
        "run_id": run_id,
        "log_root": str(log_root),
        "swarm_worktrees": str(SWARM_ROOT / run_id / "worktrees") if run_id else "",
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runs": list_runs()[:20],
        "workers": [worker_status(run_id, worker) for worker in ordered] if run_id else [],
    }


def log_path(run_id: str, worker: str, kind: str) -> Path:
    if not SAFE_NAME.match(run_id) or not SAFE_NAME.match(worker):
        raise ValueError("bad run or worker")
    log_dir = RUNS_ROOT / run_id / worker
    worktree = SWARM_ROOT / run_id / "worktrees" / worker
    choices = {
        "events": log_dir / "events.jsonl",
        "stderr": log_dir / "stderr.log",
        "queue": log_dir / "queue.out",
        "meta": log_dir / "meta.env",
        "final": worktree / "runs" / "codex" / f"{worker}.final.md",
    }
    path = choices.get(kind)
    if path is None:
        raise ValueError("bad log kind")
    return path


HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Swarm Ops</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101214;
      --band: #171a1d;
      --panel: #20252a;
      --panel-2: #262c31;
      --text: #e7ecef;
      --muted: #a9b4ba;
      --line: #354047;
      --ok: #42c879;
      --warn: #e1b84f;
      --bad: #ee6d6d;
      --info: #5db7de;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      background: var(--band);
      border-bottom: 1px solid var(--line);
      padding: 16px 22px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    h1 {
      font-size: 20px;
      margin: 0;
      font-weight: 650;
    }
    main {
      padding: 18px 22px 28px;
      max-width: 1480px;
      margin: 0 auto;
    }
    .topline {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) auto;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }
    .controls {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    button, select {
      height: 34px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
      font-size: 13px;
    }
    button { cursor: pointer; }
    button:hover, select:hover { border-color: var(--info); }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .worker {
      min-height: 184px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 13px;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 9px;
    }
    .worker-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .worker h2 {
      margin: 0;
      font-size: 16px;
      font-weight: 650;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 0 9px;
      font-size: 12px;
      border: 1px solid var(--line);
      color: var(--muted);
      white-space: nowrap;
    }
    .pill.running { color: #05150b; background: var(--ok); border-color: var(--ok); }
    .pill.failed { color: #210606; background: var(--bad); border-color: var(--bad); }
    .pill.done { color: #0a1114; background: var(--info); border-color: var(--info); }
    .kv {
      display: grid;
      grid-template-columns: 70px minmax(0, 1fr);
      gap: 4px 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .kv code, .path {
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      overflow-wrap: anywhere;
    }
    .message {
      color: var(--text);
      font-size: 13px;
      line-height: 1.42;
      min-height: 56px;
      max-height: 92px;
      overflow: hidden;
    }
    .worker-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .worker-actions button {
      height: 30px;
      padding: 0 8px;
      font-size: 12px;
    }
    .logbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin: 10px 0 8px;
      flex-wrap: wrap;
    }
    .logbar h2 {
      margin: 0;
      font-size: 16px;
      font-weight: 650;
    }
    pre {
      margin: 0;
      min-height: 360px;
      max-height: 58vh;
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0b0d0f;
      color: #dbe3e7;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .empty {
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 18px;
      background: var(--panel);
    }
    @media (max-width: 760px) {
      header, main { padding-left: 14px; padding-right: 14px; }
      .topline { grid-template-columns: 1fr; }
      .controls { justify-content: flex-start; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Codex Swarm Ops</h1>
    <div class="controls">
      <select id="runSelect" title="Run"></select>
      <button id="refreshBtn">Refresh</button>
      <button id="autoBtn">Auto: on</button>
    </div>
  </header>
  <main>
    <section class="topline">
      <div class="meta" id="meta">Loading...</div>
      <div class="meta" id="clock"></div>
    </section>
    <section class="grid" id="workers"></section>
    <section>
      <div class="logbar">
        <h2 id="logTitle">Log</h2>
        <div class="controls">
          <select id="kindSelect">
            <option value="events">events</option>
            <option value="stderr">stderr</option>
            <option value="queue">queue</option>
            <option value="final">final</option>
            <option value="meta">meta</option>
          </select>
        </div>
      </div>
      <pre id="log"></pre>
    </section>
  </main>
  <script>
    let state = null;
    let selectedRun = "";
    let selectedWorker = "";
    let selectedKind = "events";
    let auto = true;

    const el = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    function statusClass(worker) {
      if (worker.running) return "running";
      if (worker.exitcode && worker.exitcode !== "0") return "failed";
      if (worker.exitcode === "0" || worker.final_bytes > 0) return "done";
      return "";
    }

    function statusText(worker) {
      if (worker.running) return `running ${worker.pid || ""}`;
      if (worker.exitcode) return `exit ${worker.exitcode}`;
      return worker.status || "unknown";
    }

    async function fetchStatus() {
      const qs = selectedRun ? `?run_id=${encodeURIComponent(selectedRun)}` : "";
      const res = await fetch(`/api/status${qs}`);
      state = await res.json();
      selectedRun = state.run_id || selectedRun;
      render();
      if (!selectedWorker && state.workers.length) {
        selectedWorker = state.workers[0].worker;
      }
      await fetchLog();
    }

    function renderRuns() {
      const select = el("runSelect");
      const current = select.value || selectedRun;
      select.innerHTML = "";
      for (const run of state.runs || []) {
        const opt = document.createElement("option");
        opt.value = run.id;
        opt.textContent = run.id;
        if (run.id === current || run.id === selectedRun) opt.selected = true;
        select.appendChild(opt);
      }
    }

    function render() {
      renderRuns();
      el("clock").textContent = `Updated ${state.now}`;
      el("meta").innerHTML = [
        `<b>Run</b> <span class="path">${esc(state.run_id)}</span>`,
        `<b>Logs</b> <span class="path">${esc(state.log_root)}</span>`,
        `<b>Worktrees</b> <span class="path">${esc(state.swarm_worktrees)}</span>`
      ].join("<br>");

      const container = el("workers");
      if (!state.workers.length) {
        container.innerHTML = `<div class="empty">No workers for this run.</div>`;
        return;
      }

      container.innerHTML = state.workers.map(worker => {
        const recent = worker.recent || {};
        const cls = statusClass(worker);
        const msg = recent.last_agent || recent.last_command || "No recent event text.";
        const changed = worker.changed ? worker.changed.split("\n").length : 0;
        return `
          <article class="worker">
            <div class="worker-head">
              <h2>${esc(worker.worker)}</h2>
              <span class="pill ${cls}">${esc(statusText(worker))}</span>
            </div>
            <div class="kv">
              <span>updated</span><code>${esc(worker.updated_text || "-")}</code>
              <span>events</span><code>${worker.events_bytes || worker.queue_bytes || 0} bytes</code>
              <span>changes</span><code>${changed}</code>
            </div>
            <div class="message">${esc(msg)}</div>
            <div class="worker-actions">
              <button data-worker="${esc(worker.worker)}" data-kind="events">events</button>
              <button data-worker="${esc(worker.worker)}" data-kind="stderr">stderr</button>
              <button data-worker="${esc(worker.worker)}" data-kind="queue">queue</button>
              <button data-worker="${esc(worker.worker)}" data-kind="final">final</button>
            </div>
          </article>
        `;
      }).join("");

      for (const btn of container.querySelectorAll("button")) {
        btn.addEventListener("click", async () => {
          selectedWorker = btn.dataset.worker;
          selectedKind = btn.dataset.kind;
          el("kindSelect").value = selectedKind;
          await fetchLog();
        });
      }
    }

    async function fetchLog() {
      if (!selectedRun || !selectedWorker) return;
      const kind = selectedKind || el("kindSelect").value || "events";
      const qs = new URLSearchParams({run_id: selectedRun, worker: selectedWorker, kind, lines: "160"});
      const res = await fetch(`/api/log?${qs}`);
      const text = await res.text();
      el("logTitle").textContent = `${selectedWorker} / ${kind}`;
      el("log").textContent = text || "No log content.";
    }

    el("refreshBtn").addEventListener("click", fetchStatus);
    el("autoBtn").addEventListener("click", () => {
      auto = !auto;
      el("autoBtn").textContent = `Auto: ${auto ? "on" : "off"}`;
    });
    el("runSelect").addEventListener("change", async (ev) => {
      selectedRun = ev.target.value;
      selectedWorker = "";
      await fetchStatus();
    });
    el("kindSelect").addEventListener("change", async (ev) => {
      selectedKind = ev.target.value;
      await fetchLog();
    });
    setInterval(() => { if (auto) fetchStatus(); }, 5000);
    fetchStatus();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(data, indent=2).encode(), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.send_bytes(HTML.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                self.send_json(collect_status(query.get("run_id", [""])[0] or None))
            elif parsed.path == "/api/runs":
                self.send_json(list_runs())
            elif parsed.path == "/api/log":
                run_id = query.get("run_id", [""])[0] or latest_run_id()
                worker = query.get("worker", ["queue"])[0]
                kind = query.get("kind", ["events"])[0]
                lines = int(query.get("lines", ["160"])[0])
                path = log_path(run_id, worker, kind)
                text = tail_lines(path, lines=max(1, min(lines, 1000)), max_bytes=1_000_000)
                self.send_bytes(text.encode(), "text/plain; charset=utf-8")
            else:
                self.send_bytes(b"not found", "text/plain; charset=utf-8", 404)
        except Exception as exc:
            self.send_json({"error": html.escape(str(exc))}, 500)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard: http://{args.host}:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
