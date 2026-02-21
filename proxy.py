#!/usr/bin/env python3
"""
AniWorld Proxy Server + Dashboard
- /play/{slug}/{season}/{episode} - Stream-Redirect für Emby (.strm)
- / - Web Dashboard (Status, Sync, Config)
- /api/dashboard/* - Dashboard API
"""

import asyncio
import configparser
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

# Config
CONFIG_PATH = os.environ.get("ANIWORLD_CONFIG", "/etc/aniworld/config.ini")
SYNC_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.py")

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

API_PORT = config.getint("api", "port", fallback=5080)
META_PORT = config.getint("metadata", "port", fallback=5090)
PROXY_PORT = config.getint("proxy", "port", fallback=5081)
PREF_LANGUAGE = config.get("preferences", "language", fallback="Deutsch")
PREF_HOSTER = config.get("preferences", "hoster", fallback="VOE")

API_BASE = f"http://localhost:{API_PORT}"
META_BASE = f"http://localhost:{META_PORT}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("aniworld-proxy")

app = FastAPI(title="AniWorld Proxy", docs_url=None, redoc_url=None)

# --- Sync Process State ---
sync_process = None
sync_log = []
sync_start_time = None
sync_end_time = None
sync_exit_code = None
MAX_LOG_LINES = 500


def _read_sync_output():
    """Background thread: liest sync stdout/stderr in sync_log."""
    global sync_process, sync_end_time, sync_exit_code
    if not sync_process:
        return
    for line in iter(sync_process.stdout.readline, ''):
        if line:
            sync_log.append(line.rstrip('\n'))
            if len(sync_log) > MAX_LOG_LINES:
                sync_log.pop(0)
    sync_process.wait()
    sync_exit_code = sync_process.returncode
    sync_end_time = datetime.now().isoformat()
    sync_process = None


# ========================
# Stream Proxy Endpoints
# ========================

@app.get("/play/{slug}/{season}/{episode}")
async def play(slug: str, season: int, episode: int):
    log.info(f"Play request: {slug} S{season}E{episode}")
    try:
        resp = requests.post(
            f"{API_BASE}/api/resolve",
            json={"slug": slug, "season": season, "episode": episode},
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.error(f"API request failed: {e}")
        raise HTTPException(status_code=502, detail="API server unreachable")

    if not data or not isinstance(data, list) or len(data) == 0:
        raise HTTPException(status_code=404, detail="No streams found")

    lang_priority = {"Deutsch": 0, "GerSub": 1, "EngSub": 2}
    hoster_priority = ["VOE", "Vidmoly", "Doodstream", "Streamtape", "Filemoon"]

    def sort_key(stream):
        lang_idx = lang_priority.get(stream.get("language", ""), 99)
        try:
            hoster_idx = hoster_priority.index(stream.get("name", ""))
        except ValueError:
            hoster_idx = 99
        return (lang_idx, hoster_idx)

    streams = sorted(data, key=sort_key)
    best = streams[0]
    stream_url = best.get("streamUrl", "")
    if not stream_url:
        raise HTTPException(status_code=404, detail="Stream URL empty")

    log.info(f"Redirecting to {best.get('name')} ({best.get('language')})")
    return RedirectResponse(url=stream_url, status_code=302)


@app.get("/health")
async def health():
    return {"status": "ok", "api": API_BASE}


# ========================
# Dashboard API
# ========================

@app.get("/api/dashboard/status")
async def dashboard_status():
    """Status aller Services checken."""
    services = {}

    # API Server
    try:
        r = requests.get(f"{API_BASE}/api/status", timeout=3)
        services["api"] = {"status": "online", "port": API_PORT, "detail": r.json() if r.ok else {}}
    except Exception:
        services["api"] = {"status": "offline", "port": API_PORT}

    # Metadata Server
    try:
        r = requests.get(f"{META_BASE}/health", timeout=3)
        services["metadata"] = {"status": "online", "port": META_PORT}
    except Exception:
        services["metadata"] = {"status": "offline", "port": META_PORT}

    # Proxy (wir selbst)
    services["proxy"] = {"status": "online", "port": PROXY_PORT}

    # Sync
    if sync_process and sync_process.poll() is None:
        services["sync"] = {"status": "running", "started": sync_start_time}
    elif sync_exit_code is not None:
        services["sync"] = {
            "status": "finished",
            "exit_code": sync_exit_code,
            "started": sync_start_time,
            "ended": sync_end_time
        }
    else:
        services["sync"] = {"status": "idle"}

    return services


@app.post("/api/dashboard/sync/start")
async def sync_start():
    """Sync starten."""
    global sync_process, sync_log, sync_start_time, sync_end_time, sync_exit_code
    if sync_process and sync_process.poll() is None:
        raise HTTPException(status_code=409, detail="Sync läuft bereits")

    sync_log.clear()
    sync_start_time = datetime.now().isoformat()
    sync_end_time = None
    sync_exit_code = None

    sync_process = subprocess.Popen(
        [sys.executable, SYNC_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "ANIWORLD_CONFIG": CONFIG_PATH}
    )

    import threading
    threading.Thread(target=_read_sync_output, daemon=True).start()

    return {"status": "started"}


@app.post("/api/dashboard/sync/stop")
async def sync_stop():
    """Sync stoppen."""
    global sync_process
    if not sync_process or sync_process.poll() is not None:
        raise HTTPException(status_code=409, detail="Kein Sync aktiv")

    sync_process.terminate()
    try:
        sync_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        sync_process.kill()

    return {"status": "stopped"}


@app.get("/api/dashboard/sync/log")
async def sync_get_log(offset: int = 0):
    """Sync-Log ab offset zurückgeben."""
    return {"lines": sync_log[offset:], "total": len(sync_log), "offset": offset}


@app.get("/api/dashboard/config")
async def config_get():
    """Config-Datei lesen."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return {"path": CONFIG_PATH, "content": f.read()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/dashboard/config")
async def config_save(request: Request):
    """Config-Datei speichern."""
    body = await request.json()
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Config darf nicht leer sein")

    # Validieren
    test = configparser.ConfigParser()
    try:
        test.read_string(content)
    except configparser.Error as e:
        raise HTTPException(status_code=400, detail=f"Ungültige Config: {e}")

    try:
        with open(CONFIG_PATH, 'w') as f:
            f.write(content)
        return {"status": "saved", "path": CONFIG_PATH}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/dashboard/incremental-sync")
async def incremental_sync():
    """Incremental Sync über API-Server starten (nur Änderungen)."""
    try:
        r = requests.post(f"{API_BASE}/api/sync/incremental", timeout=10)
        if r.ok:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except requests.ConnectionError:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


@app.post("/api/dashboard/detail-scrape/batch")
async def detail_scrape_batch():
    """Batch Detail-Scrape über API-Server starten."""
    try:
        r = requests.post(f"{API_BASE}/api/sync/details", timeout=10)
        if r.ok:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except requests.ConnectionError:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


@app.post("/api/dashboard/detail-scrape/{slug}")
async def detail_scrape_single(slug: str):
    """Einzelnes Anime detail-scrapen über API-Server."""
    try:
        r = requests.post(f"{API_BASE}/api/scrape/detail/{slug}", timeout=30)
        data = r.json()
        if r.ok:
            return data
        raise HTTPException(status_code=r.status_code, detail=data.get("error", "Fehler"))
    except requests.ConnectionError:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


# ========================
# Dashboard UI
# ========================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AniWorld Dashboard</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e4e4e7; --muted: #888; --accent: #6c5ce7;
    --green: #00b894; --red: #e17055; --yellow: #fdcb6e; --blue: #74b9ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, 'Segoe UI', Roboto, monospace;
    background: var(--bg); color: var(--text);
    padding: 20px; max-width: 1200px; margin: 0 auto;
  }
  h1 { font-size: 1.5rem; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
  h1 span { font-size: 1.8rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card h3 { font-size: 0.85rem; color: var(--muted); text-transform: uppercase; margin-bottom: 8px; }
  .status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
  .status-dot.online, .status-dot.running { background: var(--green); }
  .status-dot.offline { background: var(--red); }
  .status-dot.idle, .status-dot.finished { background: var(--yellow); }
  .port { color: var(--muted); font-size: 0.8rem; }
  .section { margin-bottom: 24px; }
  .section h2 { font-size: 1.1rem; margin-bottom: 12px; }
  .btn { padding: 8px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.9rem; font-weight: 600; transition: opacity 0.2s; }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-start { background: var(--green); color: #000; }
  .btn-stop { background: var(--red); color: #fff; }
  .btn-save { background: var(--accent); color: #fff; }
  .btn-group { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .log-box {
    background: #000; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; height: 40vh; min-height: 150px; max-height: 500px;
    overflow-y: auto; font-family: 'Fira Code', monospace;
    font-size: 0.8rem; line-height: 1.5; white-space: pre-wrap; color: #aaa;
  }
  .log-box .info { color: var(--blue); }
  .log-box .error { color: var(--red); }
  .log-box .warn { color: var(--yellow); }
  textarea {
    width: 100%; min-height: 200px; height: 30vh; max-height: 400px;
    background: #000; color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 12px;
    font-family: 'Fira Code', monospace; font-size: 0.85rem; resize: vertical;
  }
  .toast {
    position: fixed; bottom: 20px; right: 20px; padding: 12px 20px;
    border-radius: 8px; font-size: 0.9rem; opacity: 0; transition: opacity 0.3s;
    z-index: 999;
  }
  .toast.show { opacity: 1; }
  .toast.ok { background: var(--green); color: #000; }
  .toast.err { background: var(--red); color: #fff; }

  /* Mobile */
  @media (max-width: 600px) {
    body { padding: 12px; }
    h1 { font-size: 1.2rem; }
    .grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
    .btn { padding: 8px 14px; font-size: 0.8rem; }
    .btn-group { gap: 6px; }
    .log-box { height: 30vh; font-size: 0.7rem; }
    textarea { height: 25vh; font-size: 0.75rem; }
  }

  @media (max-width: 400px) {
    .grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<h1><span>🎬</span> AniWorld Dashboard</h1>

<!-- Status Cards -->
<div class="grid" id="status-grid"></div>

<!-- Aniworld Scrape -->
<div class="section">
  <h2>Aniworld Scrape</h2>
  <div class="btn-group" style="align-items:center; flex-wrap:wrap; gap:8px;">
    <button class="btn btn-start" id="btn-incremental" onclick="incrementalSync()">🔄 Änderungen scrapen</button>
    <span style="color:var(--muted); font-size:0.85rem;">(neue Serien + Episoden von aniworld.to)</span>
  </div>
  <div id="incremental-result" style="margin-top:10px; font-size:0.85rem; color:var(--muted);"></div>
</div>

<!-- Sync Control -->
<div class="section">
  <h2>Sync</h2>
  <div class="btn-group">
    <button class="btn btn-start" id="btn-sync-start" onclick="syncStart()">▶ Starten</button>
    <button class="btn btn-stop" id="btn-sync-stop" onclick="syncStop()" disabled>⬛ Stoppen</button>
  </div>
  <div class="log-box" id="sync-log"></div>
</div>

<!-- Detail Scrape -->
<div class="section">
  <h2>Detail Scrape</h2>
  <div id="scrape-status" style="margin-bottom:12px; padding:12px 16px; background:var(--surface); border:1px solid var(--border); border-radius:8px; font-size:0.9rem;"></div>
  <div class="btn-group" style="align-items:center; flex-wrap:wrap; gap:8px;">
    <button class="btn btn-start" id="btn-detail-batch" onclick="detailBatch()">🔄 Batch Scrape</button>
    <span style="color:var(--muted);margin:0 4px;">oder</span>
    <input type="text" id="detail-slug" placeholder="anime-slug eingeben..." style="
      padding:8px 12px; border:1px solid var(--border); border-radius:6px;
      background:var(--surface); color:var(--text); font-size:0.9rem;
      width:220px; max-width:100%; flex:1; min-width:150px;
    ">
    <button class="btn btn-save" onclick="detailSingle()">🔍 Einzeln Scrapen</button>
  </div>
  <div id="detail-result" style="margin-top:10px; font-size:0.85rem; color:var(--muted);"></div>
</div>

<!-- Config Editor -->
<div class="section">
  <h2>Konfiguration <span style="color:var(--muted);font-size:0.8rem" id="config-path"></span></h2>
  <textarea id="config-editor" spellcheck="false"></textarea>
  <div style="margin-top:8px">
    <button class="btn btn-save" onclick="configSave()">💾 Speichern</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = '';
let logOffset = 0;
let logInterval = null;

function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = 'toast', 3000);
}

async function fetchStatus() {
  try {
    const r = await fetch(API + '/api/dashboard/status');
    const data = await r.json();
    renderStatus(data);
  } catch(e) { console.error(e); }
}

function renderStatus(data) {
  const grid = document.getElementById('status-grid');
  const names = {api: 'API Server', metadata: 'Metadata Server', proxy: 'Proxy', sync: 'Sync'};
  let html = '';
  for (const [key, info] of Object.entries(data)) {
    const st = info.status || 'offline';
    html += `<div class="card">
      <h3>${names[key] || key}</h3>
      <span class="status-dot ${st}"></span>${st}
      ${info.port ? `<span class="port">:${info.port}</span>` : ''}
    </div>`;
  }
  grid.innerHTML = html;

  // Sync buttons
  const isRunning = data.sync && data.sync.status === 'running';
  document.getElementById('btn-sync-start').disabled = isRunning;
  document.getElementById('btn-sync-stop').disabled = !isRunning;

  if (isRunning && !logInterval) {
    logInterval = setInterval(fetchLog, 1500);
  } else if (!isRunning && logInterval) {
    clearInterval(logInterval);
    logInterval = null;
    fetchLog(); // letztes Update
  }

  // Scrape Status
  const apiDetail = data.api && data.api.detail;
  const scrapeBox = document.getElementById('scrape-status');
  if (apiDetail && apiDetail.animeCount !== undefined) {
    const total = apiDetail.animeCount;
    const scraped = apiDetail.detailsScraped || 0;
    const pending = apiDetail.detailsPending || 0;
    const running = apiDetail.detailSyncRunning;
    const pct = total > 0 ? Math.round((scraped / total) * 100) : 0;

    let statusIcon = running ? '🔄' : (pending === 0 ? '✅' : '⏸️');
    let statusText = running ? 'Läuft...' : (pending === 0 ? 'Komplett' : 'Pausiert');

    scrapeBox.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span>${statusIcon} <strong>Scrape Status:</strong> ${statusText}</span>
        <span style="color:var(--accent); font-weight:600;">${pct}%</span>
      </div>
      <div style="background:var(--border); border-radius:4px; height:8px; overflow:hidden;">
        <div style="background:${pending === 0 ? 'var(--green)' : 'var(--accent)'}; height:100%; width:${pct}%; transition:width 0.5s;"></div>
      </div>
      <div style="margin-top:6px; font-size:0.8rem; color:var(--muted);">
        ${scraped} / ${total} Anime gescraped — ${pending} offen
      </div>
    `;
  } else {
    scrapeBox.innerHTML = '<span style="color:var(--muted);">Scrape Status: API nicht erreichbar</span>';
  }
}

async function syncStart() {
  try {
    logOffset = 0;
    document.getElementById('sync-log').innerHTML = '';
    const r = await fetch(API + '/api/dashboard/sync/start', {method:'POST'});
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); return; }
    toast('Sync gestartet');
    fetchStatus();
    logInterval = setInterval(fetchLog, 1500);
  } catch(e) { toast('Fehler: ' + e, false); }
}

async function syncStop() {
  try {
    const r = await fetch(API + '/api/dashboard/sync/stop', {method:'POST'});
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); return; }
    toast('Sync gestoppt');
    fetchStatus();
  } catch(e) { toast('Fehler: ' + e, false); }
}

async function fetchLog() {
  try {
    const r = await fetch(API + '/api/dashboard/sync/log?offset=' + logOffset);
    const data = await r.json();
    const box = document.getElementById('sync-log');
    for (const line of data.lines) {
      const span = document.createElement('div');
      if (line.includes('[ERROR]')) span.className = 'error';
      else if (line.includes('[WARNING]')) span.className = 'warn';
      else if (line.includes('[INFO]')) span.className = 'info';
      span.textContent = line;
      box.appendChild(span);
    }
    logOffset = data.total;
    box.scrollTop = box.scrollHeight;
  } catch(e) {}
}

async function loadConfig() {
  try {
    const r = await fetch(API + '/api/dashboard/config');
    const data = await r.json();
    document.getElementById('config-editor').value = data.content;
    document.getElementById('config-path').textContent = data.path;
  } catch(e) { toast('Config laden fehlgeschlagen', false); }
}

async function configSave() {
  const content = document.getElementById('config-editor').value;
  try {
    const r = await fetch(API + '/api/dashboard/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content})
    });
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); return; }
    toast('Config gespeichert!');
  } catch(e) { toast('Fehler: ' + e, false); }
}

async function incrementalSync() {
  const btn = document.getElementById('btn-incremental');
  btn.disabled = true;
  document.getElementById('incremental-result').textContent = 'Scrape läuft...';
  try {
    const r = await fetch(API + '/api/dashboard/incremental-sync', {method:'POST'});
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); return; }
    toast('Änderungen werden gescraped (ca. 5-15 Min.)');
    document.getElementById('incremental-result').innerHTML =
      '✅ Incremental Sync gestartet - neue Serien + Episoden werden geprüft (ca. 5-15 Min.)';
    fetchStatus();
  } catch(e) { toast('Fehler: ' + e, false); }
  finally { setTimeout(() => btn.disabled = false, 5000); }
}

async function detailBatch() {
  const btn = document.getElementById('btn-detail-batch');
  btn.disabled = true;
  document.getElementById('detail-result').textContent = 'Batch Detail-Scrape wird gestartet...';
  try {
    const r = await fetch(API + '/api/dashboard/detail-scrape/batch', {method:'POST'});
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); return; }
    const data = await r.json();
    toast('Batch Detail-Scrape gestartet!');
    document.getElementById('detail-result').innerHTML =
      `✅ Gestartet (Batch-Größe: ${data.batchSize || '?'})`;
    fetchStatus();
  } catch(e) { toast('Fehler: ' + e, false); }
  finally { btn.disabled = false; }
}

async function detailSingle() {
  const slug = document.getElementById('detail-slug').value.trim();
  if (!slug) { toast('Bitte einen Slug eingeben!', false); return; }
  document.getElementById('detail-result').textContent = `Scrape "${slug}"...`;
  try {
    const r = await fetch(API + '/api/dashboard/detail-scrape/' + encodeURIComponent(slug), {method:'POST'});
    const data = await r.json();
    if (!r.ok) { toast(data.detail || data.error || 'Fehler', false); document.getElementById('detail-result').textContent = '❌ ' + (data.detail || data.error); return; }
    toast(`${data.title || slug} gescraped!`);
    document.getElementById('detail-result').innerHTML =
      `✅ <strong>${data.title || slug}</strong> erfolgreich gescraped`;
  } catch(e) { toast('Fehler: ' + e, false); document.getElementById('detail-result').textContent = '❌ ' + e; }
}

// Init
fetchStatus();
loadConfig();
setInterval(fetchStatus, 5000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


# ========================

if __name__ == "__main__":
    log.info(f"Starting AniWorld Proxy + Dashboard on port {PROXY_PORT}")
    log.info(f"API Server: {API_BASE} | Metadata: {META_BASE}")
    log.info(f"Dashboard: http://localhost:{PROXY_PORT}/")
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="info")
