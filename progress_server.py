#!/usr/bin/env python3
"""
progress_server.py — live translation progress in your browser.

Run alongside translate_runner.py (separate terminal):

    python progress_server.py

Then open:  http://127.0.0.1:8765/
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(PROJECT_DIR, "section_map.json")
OUTPUT_FILE = os.path.join(PROJECT_DIR, "translation_output.txt")
SKIP_FILE = os.path.join(PROJECT_DIR, "skipped_sections.txt")
LOWRATIO_FILE = os.path.join(PROJECT_DIR, "low_ratio_sections.txt")
VOICE_LOG_FILE = os.path.join(PROJECT_DIR, "voice_log.txt")

HOST = "127.0.0.1"
PORT = 8765

OUTPUT_SECTION_RE = re.compile(r"=== (.+?) — Section (\d+) ===")
SKIP_BLOCK_RE = re.compile(r"<<<BLOCKED id=(\d+)>>>")
VOICE_LOG_RE = re.compile(r"^Section (\d+) \| (.+?) \| (.+)$")

_rate_sample: dict[str, Any] = {"t": 0.0, "done": 0}


def read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def file_mtime(path: str) -> float | None:
    if not os.path.isfile(path):
        return None
    return os.path.getmtime(path)


def gather_progress() -> dict[str, Any]:
    total = 0
    map_sections: list[dict[str, Any]] = []
    if os.path.isfile(MAP_FILE):
        data = json.loads(read_text(MAP_FILE))
        total = int(data.get("section_count", 0))
        map_sections = data.get("sections", [])

    output = read_text(OUTPUT_FILE)
    done_set: set[int] = set()
    low_ratio_set: set[int] = set()
    last_heading = ""
    last_n = 0
    for m in OUTPUT_SECTION_RE.finditer(output):
        n = int(m.group(2))
        done_set.add(n)
        last_n = n
        last_heading = m.group(1).strip()

    skipped_set: set[int] = set()
    for m in SKIP_BLOCK_RE.finditer(read_text(SKIP_FILE)):
        skipped_set.add(int(m.group(1)))

    for line in read_text(LOWRATIO_FILE).splitlines():
        line = line.strip()
        if line.isdigit():
            low_ratio_set.add(int(line))

    done = len(done_set)
    pending_nums = [
        n for n in range(1, total + 1) if n not in done_set and n not in skipped_set
    ]
    next_n = pending_nums[0] if pending_nums else None

    voices: list[dict[str, str]] = []
    for line in read_text(VOICE_LOG_FILE).splitlines():
        m = VOICE_LOG_RE.match(line.strip())
        if m:
            voices.append({"n": m.group(1), "heading": m.group(2), "voice": m.group(3)})

    pct = (100.0 * done / total) if total else 0.0

    now = time.time()
    eta_sec: float | None = None
    if _rate_sample["t"] and done > _rate_sample["done"] and pending_nums:
        elapsed = now - _rate_sample["t"]
        delta = done - _rate_sample["done"]
        if elapsed > 0 and delta > 0:
            eta_sec = (elapsed / delta) * len(pending_nums)
    _rate_sample["t"] = now
    _rate_sample["done"] = done

    def fmt_eta(seconds: float | None) -> str:
        if seconds is None:
            return "…"
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

    out_mtime = file_mtime(OUTPUT_FILE)
    running = bool(
        pending_nums and out_mtime and (now - out_mtime) < 180
    )

    # Compact status per section for the visual map
    section_rows: list[dict[str, Any]] = []
    for sec in map_sections:
        n = int(sec["n"])
        heading = sec.get("heading", "")
        if n in done_set:
            st = "low" if n in low_ratio_set else "done"
        elif n in skipped_set:
            st = "skipped"
        elif n == next_n and running:
            st = "active"
        elif n == next_n:
            st = "next"
        else:
            st = "pending"
        section_rows.append({"n": n, "heading": heading, "status": st})

    headings = {int(s["n"]): s.get("heading", "") for s in map_sections}

    return {
        "total": total,
        "done": done,
        "skipped": len(skipped_set),
        "low_ratio": len(low_ratio_set),
        "pending": len(pending_nums),
        "percent": round(pct, 1),
        "running": running,
        "last_section_n": last_n,
        "last_heading": last_heading,
        "next_section_n": next_n,
        "next_heading": headings.get(next_n, "") if next_n else "",
        "eta": fmt_eta(eta_sec),
        "sections": section_rows,
        "recent_voices": voices[-6:],
        "updated_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Wikowí — Translation Progress</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: "Segoe UI", system-ui, sans-serif;
      background: #0d0f14;
      color: #e8eaed;
      margin: 0;
      padding: 1.5rem;
      min-height: 100vh;
    }
    .wrap { max-width: 900px; margin: 0 auto; }
    header { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
    h1 { font-size: 1.15rem; font-weight: 600; margin: 0; }
    .live { display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; color: #9aa0a6; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
    .dot.on { background: #34d399; animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }

    .hero { display: flex; gap: 2rem; align-items: center; flex-wrap: wrap; margin-bottom: 1.5rem; }
    .ring-wrap { position: relative; width: 120px; height: 120px; flex-shrink: 0; }
    .ring-wrap svg { transform: rotate(-90deg); }
    .ring-center {
      position: absolute; inset: 0; display: flex; flex-direction: column;
      align-items: center; justify-content: center;
    }
    .ring-center b { font-size: 1.6rem; line-height: 1; }
    .ring-center span { font-size: 0.7rem; color: #9aa0a6; margin-top: 2px; }

    .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.6rem; flex: 1; min-width: 240px; }
    .stat { background: #161a22; border: 1px solid #252a33; border-radius: 10px; padding: 0.65rem 0.75rem; }
    .stat b { display: block; font-size: 1.35rem; font-weight: 600; }
    .stat label { font-size: 0.7rem; color: #9aa0a6; text-transform: uppercase; letter-spacing: 0.04em; }

    .now { background: #161a22; border: 1px solid #252a33; border-radius: 10px; padding: 1rem 1.1rem; margin-bottom: 1.25rem; }
    .now-row { margin: 0.35rem 0; font-size: 0.9rem; }
    .now-row strong { color: #9aa0a6; font-weight: 500; margin-right: 0.35rem; }
    .active-label { color: #60a5fa; }

    .map-title { font-size: 0.75rem; color: #9aa0a6; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.6rem; }
    .map-legend { display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.72rem; color: #9aa0a6; margin-bottom: 0.75rem; }
    .map-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(14px, 1fr));
      gap: 4px;
      margin-bottom: 1rem;
    }
    .cell {
      aspect-ratio: 1;
      border-radius: 3px;
      background: #252a33;
      cursor: default;
      transition: transform 0.15s, box-shadow 0.15s;
    }
    .cell:hover { transform: scale(1.35); z-index: 1; box-shadow: 0 0 0 1px #fff3; }
    .cell.done { background: #059669; }
    .cell.low { background: #d97706; }
    .cell.skipped { background: #dc2626; }
    .cell.next { background: #374151; outline: 1px solid #6b7280; }
    .cell.active { background: #2563eb; animation: pulse 1.2s infinite; }
    .cell.pending { background: #252a33; }

    #tooltip {
      position: fixed; display: none; background: #1f2937; border: 1px solid #374151;
      padding: 0.45rem 0.65rem; border-radius: 6px; font-size: 0.75rem; max-width: 280px;
      pointer-events: none; z-index: 99; line-height: 1.35;
    }
    footer { font-size: 0.72rem; color: #6b7280; margin-top: 0.5rem; }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Bernal Díaz — Tomo I</h1>
      <div class="live"><span class="dot" id="dot"></span><span id="live-label">connecting…</span></div>
    </header>

    <div class="hero">
      <div class="ring-wrap">
        <svg width="120" height="120" viewBox="0 0 120 120">
          <circle cx="60" cy="60" r="52" fill="none" stroke="#252a33" stroke-width="10"/>
          <circle id="ring" cx="60" cy="60" r="52" fill="none" stroke="#4a9eff" stroke-width="10"
            stroke-linecap="round" stroke-dasharray="326.7" stroke-dashoffset="326.7"/>
        </svg>
        <div class="ring-center"><b id="pct-num">0</b><span>percent</span></div>
      </div>
      <div class="stats">
        <div class="stat"><b id="s-done">—</b><label>done</label></div>
        <div class="stat"><b id="s-left">—</b><label>left</label></div>
        <div class="stat"><b id="s-skip">—</b><label>skipped</label></div>
        <div class="stat"><b id="s-eta">—</b><label>ETA</label></div>
      </div>
    </div>

    <div class="now">
      <div class="now-row"><strong>Last finished</strong><span id="last">—</span></div>
      <div class="now-row"><strong>Working on</strong><span id="active" class="active-label">—</span></div>
      <div class="now-row"><strong>Up next</strong><span id="next">—</span></div>
    </div>

    <div class="map-title">Section map (142 chapters)</div>
    <div class="map-legend">
      <span><i style="background:#059669"></i>done</span>
      <span><i style="background:#2563eb"></i>in progress</span>
      <span><i style="background:#374151;border:1px solid #6b7280"></i>next</span>
      <span><i style="background:#252a33"></i>pending</span>
      <span><i style="background:#dc2626"></i>skipped</span>
      <span><i style="background:#d97706"></i>low ratio</span>
    </div>
    <div class="grid" id="grid"></div>
    <footer id="foot">Updated —</footer>
  </div>
  <div id="tooltip"></div>

  <script>
    const CIRC = 326.7;
    const tip = document.getElementById('tooltip');

    function esc(s) {
      const d = document.createElement('div');
      d.textContent = s || '';
      return d.innerHTML;
    }

    function render(d) {
      document.getElementById('pct-num').textContent = d.percent;
      document.getElementById('ring').style.strokeDashoffset = CIRC * (1 - d.percent / 100);
      document.getElementById('s-done').textContent = d.done;
      document.getElementById('s-left').textContent = d.pending;
      document.getElementById('s-skip').textContent = d.skipped;
      document.getElementById('s-eta').textContent = d.eta;

      const dot = document.getElementById('dot');
      const lbl = document.getElementById('live-label');
      if (d.pending === 0) {
        dot.className = 'dot';
        lbl.textContent = 'complete';
      } else if (d.running) {
        dot.className = 'dot on';
        lbl.textContent = 'translating…';
      } else {
        dot.className = 'dot';
        lbl.textContent = 'paused (no recent output)';
      }

      document.getElementById('last').textContent = d.last_section_n
        ? `§${d.last_section_n} — ${d.last_heading}` : '—';
      document.getElementById('active').textContent = (d.running && d.next_section_n)
        ? `§${d.next_section_n} — ${d.next_heading}` : (d.pending ? 'waiting…' : '—');
      document.getElementById('next').textContent = d.next_section_n
        ? `§${d.next_section_n} — ${d.next_heading}` : 'all done';
      document.getElementById('foot').textContent = 'Updated ' + d.updated_at + ' · polls every 3s';

      const grid = document.getElementById('grid');
      grid.innerHTML = (d.sections || []).map(s =>
        `<div class="cell ${s.status}" data-n="${s.n}" data-h="${esc(s.heading)}"></div>`
      ).join('');

      grid.querySelectorAll('.cell').forEach(el => {
        el.onmouseenter = e => {
          tip.style.display = 'block';
          tip.innerHTML = `<b>§${el.dataset.n}</b> ${el.dataset.h}<br><span style="color:#9aa0a6">${el.className.replace('cell ','')}</span>`;
        };
        el.onmousemove = e => { tip.style.left = (e.clientX+12)+'px'; tip.style.top = (e.clientY+12)+'px'; };
        el.onmouseleave = () => { tip.style.display = 'none'; };
      });
    }

    async function poll() {
      try {
        const r = await fetch('/api/progress');
        render(await r.json());
      } catch (e) { document.getElementById('live-label').textContent = 'offline'; }
    }
    poll();
    setInterval(poll, 3000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/progress":
            body = json.dumps(gather_progress(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Progress dashboard: {url}")
    print("Leave this running. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
