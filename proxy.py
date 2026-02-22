#!/usr/bin/env python3
"""
AniWorld Proxy Server + Dashboard
- /play/{slug}/{season}/{episode} - Stream-Redirect für Emby (.strm)
- / - Web Dashboard (Status, Sync, Config)
- /api/dashboard/* - Dashboard API
"""

import asyncio
import configparser
import hashlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, HTTPException, Request, Cookie
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

# ========================
# Auth System
# ========================
AUTH_FILE = os.path.join(os.path.dirname(CONFIG_PATH), "auth.json")
SESSION_TTL_HOURS = 24
_sessions = {}  # token -> expiry datetime


def _hash_password(password):
    """Hash password with SHA-256 + salt."""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return {"salt": salt, "hash": hashed}


def _verify_password(password, stored):
    """Verify password against stored hash."""
    hashed = hashlib.sha256((stored["salt"] + password).encode()).hexdigest()
    return hashed == stored["hash"]


def _load_auth():
    """Load auth config. Returns None if no auth set up."""
    try:
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_auth(auth_data):
    """Save auth config."""
    with open(AUTH_FILE, 'w') as f:
        json.dump(auth_data, f)
    os.chmod(AUTH_FILE, 0o600)


def _auth_enabled():
    """Check if auth is configured."""
    auth = _load_auth()
    return auth is not None and "hash" in auth


def _create_session():
    """Create a new session token."""
    token = secrets.token_hex(32)
    _sessions[token] = datetime.now() + timedelta(hours=SESSION_TTL_HOURS)
    # Cleanup expired sessions
    now = datetime.now()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        del _sessions[t]
    return token


def _valid_session(token):
    """Check if session token is valid."""
    if not token or token not in _sessions:
        return False
    if _sessions[token] < datetime.now():
        del _sessions[token]
        return False
    return True


def _check_auth(request: Request):
    """Check if request is authenticated. Returns True if ok, False if needs login."""
    if not _auth_enabled():
        return True  # No password set = open access
    token = request.cookies.get("aniworld_session")
    return _valid_session(token)


# ========================
# Cron Scheduler
# ========================
CRONS_FILE = os.path.join(os.path.dirname(CONFIG_PATH), "crons.json")
_cron_last_run = {}  # job_id -> last run datetime string

DEFAULT_CRONS = {
    "detail_scrape": {"name": "Detail-Scraping", "schedule": "0 */6 * * *", "enabled": True},
    "incremental_sync": {"name": "Änderungen Scrapen", "schedule": "0 2 * * *", "enabled": True},
    "strm_sync": {"name": "STRM-Sync", "schedule": "0 3 * * *", "enabled": True},
    "metadata_sync": {"name": "Metadata Sync", "schedule": "0 4 * * *", "enabled": True},
}


def _load_crons():
    """Load cron config from file, merge with defaults."""
    crons = dict(DEFAULT_CRONS)
    try:
        if os.path.exists(CRONS_FILE):
            with open(CRONS_FILE, 'r') as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in crons:
                    crons[k].update(v)
    except Exception:
        pass
    return crons


def _save_crons(crons):
    """Save cron config to file."""
    with open(CRONS_FILE, 'w') as f:
        json.dump(crons, f, indent=2)
    try:
        os.chmod(CRONS_FILE, 0o644)
    except Exception:
        pass


def _cron_matches(expr, now):
    """Check if a cron expression matches the current time (minute-level).
    Supports: * */N and specific values. Format: min hour dom month dow"""
    try:
        parts = expr.strip().split()
        if len(parts) != 5:
            return False
        fields = [now.minute, now.hour, now.day, now.month, now.weekday()]
        # weekday: cron uses 0=Sun, Python 0=Mon -> convert
        fields[4] = (now.weekday() + 1) % 7  # 0=Sun

        for i, (part, val) in enumerate(zip(parts, fields)):
            if part == '*':
                continue
            if part.startswith('*/'):
                step = int(part[2:])
                if val % step != 0:
                    return False
            elif ',' in part:
                if val not in [int(x) for x in part.split(',')]:
                    return False
            elif '-' in part:
                lo, hi = part.split('-', 1)
                if not (int(lo) <= val <= int(hi)):
                    return False
            else:
                if val != int(part):
                    return False
        return True
    except Exception:
        return False


def _run_cron_job(job_id):
    """Execute a cron job by id."""
    log.info(f"[Cron] Running job: {job_id}")
    try:
        if job_id == "detail_scrape":
            requests.post(f"{API_BASE}/api/sync/details", timeout=10)
        elif job_id == "incremental_sync":
            requests.post(f"{API_BASE}/api/sync/incremental", timeout=10)
        elif job_id == "strm_sync":
            subprocess.Popen(
                [sys.executable, SYNC_SCRIPT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "ANIWORLD_CONFIG": CONFIG_PATH}
            )
        elif job_id == "metadata_sync":
            requests.post(f"{META_BASE}/sync", timeout=10)
        _cron_last_run[job_id] = datetime.now().isoformat()
        log.info(f"[Cron] Job {job_id} started successfully")
    except Exception as e:
        log.error(f"[Cron] Job {job_id} failed: {e}")


def _cron_scheduler():
    """Background thread: checks every 60s if any cron job should run."""
    log.info("[Cron] Scheduler started")
    last_check_minute = -1
    while True:
        try:
            now = datetime.now()
            # Only check once per minute
            if now.minute != last_check_minute:
                last_check_minute = now.minute
                crons = _load_crons()
                for job_id, job in crons.items():
                    if not job.get("enabled", False):
                        continue
                    schedule = job.get("schedule", "")
                    if _cron_matches(schedule, now):
                        _run_cron_job(job_id)
        except Exception as e:
            log.error(f"[Cron] Scheduler error: {e}")
        time.sleep(15)


# Start cron scheduler thread
_cron_thread = threading.Thread(target=_cron_scheduler, daemon=True)
_cron_thread.start()

app = FastAPI(title="AniWorld Proxy", docs_url=None, redoc_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect dashboard routes. /play/*, /health, /login, /api/auth/* are open."""
    path = request.url.path
    open_paths = ["/play/", "/health", "/login", "/api/auth/"]
    if any(path.startswith(p) for p in open_paths):
        return await call_next(request)
    if path == "/" or path.startswith("/api/dashboard"):
        if not _check_auth(request):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
            return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)

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
# Auth Endpoints
# ========================

LOGIN_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AniWorld Dashboard - Login</title>
<style>
  :root { --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a; --text: #e4e4e7; --accent: #6c5ce7; --red: #e17055; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, 'Segoe UI', Roboto, monospace; background: var(--bg); color: var(--text);
    display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
  .login-box { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 32px; width: 100%; max-width: 360px; }
  h1 { font-size: 1.3rem; text-align: center; margin-bottom: 24px; }
  h1 span { font-size: 1.5rem; }
  input { width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg); color: var(--text); font-size: 0.95rem; margin-bottom: 16px; }
  input:focus { outline: none; border-color: var(--accent); }
  button { width: 100%; padding: 10px; border: none; border-radius: 6px; background: var(--accent);
    color: #fff; font-size: 0.95rem; font-weight: 600; cursor: pointer; }
  button:hover { opacity: 0.9; }
  .error { color: var(--red); font-size: 0.85rem; text-align: center; margin-bottom: 12px; display: none; }
</style>
</head>
<body>
<div class="login-box">
  <h1><span>🎬</span> AniWorld Dashboard</h1>
  <div class="error" id="error">Falsches Passwort</div>
  <form onsubmit="return login(event)">
    <input type="password" id="password" placeholder="Passwort" autofocus>
    <button type="submit">Anmelden</button>
  </form>
</div>
<script>
async function login(e) {
  e.preventDefault();
  const pw = document.getElementById('password').value;
  const r = await fetch('/api/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pw})
  });
  if (r.ok) { window.location.href = '/'; }
  else { document.getElementById('error').style.display = 'block'; }
  return false;
}
</script>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if not _auth_enabled():
        return RedirectResponse(url="/", status_code=302)
    return LOGIN_HTML


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    password = body.get("password", "")
    auth = _load_auth()
    if not auth or not _verify_password(password, auth):
        raise HTTPException(status_code=401, detail="Falsches Passwort")
    token = _create_session()
    response = JSONResponse({"status": "ok"})
    response.set_cookie("aniworld_session", token, httponly=True, max_age=SESSION_TTL_HOURS * 3600, samesite="lax")
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get("aniworld_session")
    if token in _sessions:
        del _sessions[token]
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("aniworld_session")
    return response


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    body = await request.json()
    current = body.get("current", "")
    new_pw = body.get("new", "")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 4 Zeichen haben")
    auth = _load_auth()
    if auth and not _verify_password(current, auth):
        raise HTTPException(status_code=401, detail="Aktuelles Passwort falsch")
    _save_auth(_hash_password(new_pw))
    return {"status": "ok"}


@app.post("/api/auth/set-password")
async def auth_set_password(request: Request):
    """Set initial password (only if no password configured yet)."""
    if _auth_enabled():
        raise HTTPException(status_code=403, detail="Passwort bereits gesetzt. Nutze 'Passwort ändern'.")
    body = await request.json()
    new_pw = body.get("password", "")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 4 Zeichen haben")
    _save_auth(_hash_password(new_pw))
    token = _create_session()
    response = JSONResponse({"status": "ok"})
    response.set_cookie("aniworld_session", token, httponly=True, max_age=SESSION_TTL_HOURS * 3600, samesite="lax")
    return response


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
        r = requests.get(f"{META_BASE}/status", timeout=3)
        services["metadata"] = {"status": "online", "port": META_PORT, "detail": r.json() if r.ok else {}}
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


@app.post("/api/dashboard/metadata-sync")
async def metadata_sync():
    """Metadata-Server Sync starten (AniList Metadata aktualisieren)."""
    try:
        r = requests.post(f"{META_BASE}/sync", timeout=10)
        if r.ok:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except requests.ConnectionError:
        raise HTTPException(status_code=502, detail="Metadata Server nicht erreichbar")


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


@app.get("/api/dashboard/incremental-sync/status")
async def incremental_sync_status():
    """Status des Incremental Sync vom API-Server."""
    try:
        r = requests.get(f"{API_BASE}/api/sync/incremental/status", timeout=5)
        return r.json() if r.ok else {"running": False, "result": None}
    except Exception:
        return {"running": False, "result": None}

@app.get("/api/dashboard/full-sync/status")
async def full_sync_status():
    """Status des Full Sync vom API-Server."""
    try:
        r = requests.get(f"{API_BASE}/api/sync/full/status", timeout=5)
        return r.json() if r.ok else {"running": False, "result": None}
    except Exception:
        return {"running": False, "result": None}

@app.get("/api/dashboard/recent-changes")
async def recent_changes(request: Request, days: int = 7, limit: int = 100):
    """Letzte Änderungen vom API-Server."""
    try:
        r = requests.get(f"{API_BASE}/api/changes?days={days}&limit={limit}", timeout=10)
        return JSONResponse(r.json() if r.ok else [])
    except Exception:
        return JSONResponse([])

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
# Catalog API (proxy to API server)
# ========================

@app.get("/api/dashboard/catalog/letters")
async def catalog_letters(request: Request):
    try:
        r = requests.get(f"{API_BASE}/api/letters", timeout=5)
        return JSONResponse(r.json() if r.ok else [])
    except Exception:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


@app.get("/api/dashboard/catalog/anime")
async def catalog_anime(request: Request, letter: str = "", q: str = ""):
    try:
        if q:
            r = requests.get(f"{API_BASE}/api/search", params={"q": q}, timeout=5)
        elif letter:
            r = requests.get(f"{API_BASE}/api/anime", params={"letter": letter}, timeout=5)
        else:
            r = requests.get(f"{API_BASE}/api/anime/recent", timeout=5)
        return JSONResponse(r.json() if r.ok else [])
    except Exception:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


@app.get("/api/dashboard/catalog/anime/{slug}")
async def catalog_anime_detail(slug: str, request: Request):
    try:
        r = requests.get(f"{API_BASE}/api/anime/{slug}", timeout=5)
        if not r.ok:
            raise HTTPException(status_code=404, detail="Anime nicht gefunden")
        return JSONResponse(r.json())
    except requests.ConnectionError:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


@app.get("/api/dashboard/catalog/anime/{slug}/season/{season_num}/episodes")
async def catalog_episodes(slug: str, season_num: int, request: Request):
    try:
        r = requests.get(f"{API_BASE}/api/anime/{slug}/season/{season_num}/episodes", timeout=10)
        return JSONResponse(r.json() if r.ok else [])
    except Exception:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")


# ========================
# Log Viewer API
# ========================

@app.get("/api/dashboard/logs/{service}")
async def get_service_logs(service: str, request: Request, lines: int = 100, level: str = ""):
    """Get logs for a service via journalctl."""
    allowed = {"api": "aniworld-api", "metadata": "aniworld-metadata", "proxy": "aniworld-proxy"}
    unit = allowed.get(service)
    if not unit:
        raise HTTPException(status_code=400, detail=f"Unbekannter Service: {service}")
    try:
        cmd = ["journalctl", "-u", unit, f"-n{lines}", "--no-pager", "-o", "short-iso"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        log_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        if level:
            level_upper = level.upper()
            log_lines = [l for l in log_lines if level_upper in l.upper()]
        return {"service": service, "unit": unit, "lines": log_lines, "total": len(log_lines)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dashboard/catalog/anime/{slug}/films/episodes")
async def catalog_films(slug: str, request: Request):
    try:
        r = requests.get(f"{API_BASE}/api/anime/{slug}/films/episodes", timeout=10)
        return JSONResponse(r.json() if r.ok else [])
    except Exception:
        raise HTTPException(status_code=502, detail="API Server nicht erreichbar")



# ========================
# Cron API
# ========================

@app.get("/api/dashboard/crons")
async def crons_get():
    """Get all cron jobs with last run info."""
    crons = _load_crons()
    for job_id in crons:
        crons[job_id]["last_run"] = _cron_last_run.get(job_id, None)
    return crons


@app.post("/api/dashboard/crons")
async def crons_save(request: Request):
    """Save cron job config."""
    body = await request.json()
    # Validate
    for job_id, job in body.items():
        if job_id not in DEFAULT_CRONS:
            raise HTTPException(status_code=400, detail=f"Unbekannter Job: {job_id}")
        schedule = job.get("schedule", "")
        parts = schedule.strip().split()
        if len(parts) != 5:
            raise HTTPException(status_code=400, detail=f"Ungültiger Cron-Ausdruck für {job_id}: '{schedule}' (5 Felder erwartet)")
    _save_crons(body)
    return {"status": "saved"}


@app.post("/api/dashboard/crons/{job_id}/run")
async def crons_run_now(job_id: str):
    """Manually trigger a cron job."""
    if job_id not in DEFAULT_CRONS:
        raise HTTPException(status_code=400, detail=f"Unbekannter Job: {job_id}")
    threading.Thread(target=_run_cron_job, args=(job_id,), daemon=True).start()
    return {"status": "started", "job": job_id}


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

  .tabs { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
  .tab { padding: 8px 16px; border: none; background: none; color: var(--muted); cursor: pointer;
    font-size: 0.9rem; font-weight: 600; border-bottom: 2px solid transparent; transition: all 0.2s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .anime-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; margin-bottom: 8px; cursor: pointer; transition: border-color 0.2s; }
  .anime-card:hover { border-color: var(--accent); }
  .anime-card h3 { font-size: 0.95rem; margin-bottom: 4px; }
  .anime-card .meta { font-size: 0.8rem; color: var(--muted); }
  .letter-btn { padding: 4px 10px; border: 1px solid var(--border); border-radius: 4px;
    background: var(--surface); color: var(--text); cursor: pointer; font-size: 0.8rem; font-weight: 600; }
  .letter-btn:hover, .letter-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .back-btn { color: var(--accent); cursor: pointer; font-size: 0.9rem; margin-bottom: 12px; display: inline-block; }
  .back-btn:hover { text-decoration: underline; }
  .episode-row { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 0.85rem; }
  .episode-row:last-child { border-bottom: none; }

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
<div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:20px;">
  <h1 style="margin-bottom:0;"><span>🎬</span> AniWorld Dashboard</h1>
  <button class="btn btn-stop" onclick="logout()" style="width:auto; font-size:0.8rem; padding:6px 14px;">🚪 Abmelden</button>
</div>

<!-- Tab Navigation -->
<div class="tabs">
  <button class="tab active" onclick="switchTab('dashboard')">📊 Dashboard</button>
  <button class="tab" onclick="switchTab('recent')">🆕 Neu</button>
  <button class="tab" onclick="switchTab('catalog')">🔍 Katalog</button>
  <button class="tab" onclick="switchTab('config')">⚙️ Konfiguration</button>
  <button class="tab" onclick="switchTab('crons')">⏰ Crons</button>
  <button class="tab" onclick="switchTab('logs')">📋 Logs</button>
  <button class="tab" style="margin-left:auto;" onclick="switchTab('settings')">🔧 Einstellungen</button>
</div>

<!-- Tab: Dashboard -->
<div id="tab-dashboard">

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

<!-- Metadata Sync -->
<div class="section">
  <h2>Metadata Server</h2>
  <div id="meta-status" style="margin-bottom:12px; padding:12px 16px; background:var(--surface); border:1px solid var(--border); border-radius:8px; font-size:0.9rem;"></div>
  <div class="btn-group" style="align-items:center; flex-wrap:wrap; gap:8px;">
    <button class="btn btn-start" id="btn-meta-sync" onclick="metadataSync()">🔄 Metadata aktualisieren</button>
    <span style="color:var(--muted); font-size:0.85rem;">(Cover, Beschreibungen, Genres von AniList/MAL)</span>
  </div>
  <div id="meta-result" style="margin-top:10px; font-size:0.85rem; color:var(--muted);"></div>
</div>

<!-- Sync Control -->
<div class="section">
  <h2>STRM-Sync</h2>
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
    <button class="btn btn-save" id="btn-detail-single" onclick="detailSingle()">🔍 Einzeln Scrapen</button>
  </div>
  <div id="detail-result" style="margin-top:10px; font-size:0.85rem; color:var(--muted);"></div>
</div>


</div><!-- /tab-dashboard -->

<!-- Tab: Zuletzt hinzugefügt -->
<div id="tab-recent" style="display:none;">
  <h2>🆕 Zuletzt hinzugefügt</h2>
  <div style="margin-bottom:15px;">
    <select id="recent-days" onchange="loadRecentChanges()" style="padding:6px 12px; background:var(--card); color:var(--text); border:1px solid var(--border); border-radius:6px;">
      <option value="1">Letzte 24h</option>
      <option value="3">Letzte 3 Tage</option>
      <option value="7" selected>Letzte 7 Tage</option>
      <option value="30">Letzte 30 Tage</option>
    </select>
  </div>
  <div id="recent-list" style="font-size:0.9rem;"></div>
</div><!-- /tab-recent -->

<!-- Tab: Konfiguration -->
<div id="tab-config" style="display:none;">
<div class="section">
  <h2>Konfiguration <span style="color:var(--muted);font-size:0.8rem" id="config-path"></span></h2>
  <textarea id="config-editor" spellcheck="false"></textarea>
  <div style="margin-top:8px">
    <button class="btn btn-save" onclick="configSave()">💾 Speichern</button>
  </div>
</div>
</div><!-- /tab-config -->

<!-- Tab: Einstellungen -->
<div id="tab-settings" style="display:none;">
<div class="section">
  <h2>Einstellungen</h2>
  <div style="background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:16px;">
    <h3 style="font-size:0.9rem; color:var(--muted); margin-bottom:12px;">PASSWORT</h3>
    <div id="pw-section"></div>
  </div>
</div>
</div><!-- /tab-settings -->

<!-- Tab: Katalog -->
<div id="tab-catalog" style="display:none;">
  <div class="section">
    <div style="display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap;">
      <input type="text" id="catalog-search" placeholder="Anime suchen..." onkeyup="if(event.key==='Enter')catalogSearch()" style="
        padding:8px 12px; border:1px solid var(--border); border-radius:6px;
        background:var(--surface); color:var(--text); font-size:0.9rem; flex:1; min-width:200px;">
      <button class="btn btn-save" onclick="catalogSearch()">🔍 Suchen</button>
    </div>
    <div id="catalog-letters" style="display:flex; flex-wrap:wrap; gap:4px; margin-bottom:16px;"></div>
    <div id="catalog-info" style="font-size:0.85rem; color:var(--muted); margin-bottom:12px;"></div>
    <div id="catalog-list"></div>
    <div id="catalog-detail" style="display:none;"></div>
  </div>
</div><!-- /tab-catalog -->

<!-- Tab: Crons -->
<div id="tab-crons" style="display:none;">
<div class="section">
  <h2>Cronjobs</h2>
  <p style="color:var(--muted); font-size:0.85rem; margin-bottom:16px;">
    Zeitgesteuerte Aufgaben. Format: <code style="background:var(--surface); padding:2px 6px; border-radius:3px;">Min Std Tag Monat Wochentag</code>
    - z.B. <code style="background:var(--surface); padding:2px 6px; border-radius:3px;">0 3 * * *</code> = täglich 03:00,
    <code style="background:var(--surface); padding:2px 6px; border-radius:3px;">0 */6 * * *</code> = alle 6 Stunden
  </p>
  <div id="crons-list"></div>
  <div style="margin-top:12px;">
    <button class="btn btn-save" onclick="cronsSave()">💾 Speichern</button>
    <span id="crons-result" style="margin-left:12px; font-size:0.85rem; color:var(--muted);"></span>
  </div>
</div>
</div><!-- /tab-crons -->

<!-- Tab: Logs -->
<div id="tab-logs" style="display:none;">
  <div class="section">
    <div style="display:flex; gap:8px; margin-bottom:12px; flex-wrap:wrap; align-items:center;">
      <button class="letter-btn active" onclick="loadLogs('api', this)">API Server</button>
      <button class="letter-btn" onclick="loadLogs('metadata', this)">Metadata Server</button>
      <button class="letter-btn" onclick="loadLogs('proxy', this)">Proxy</button>
      <span style="color:var(--muted); margin-left:8px;">Filter:</span>
      <select id="log-level" onchange="reloadLogs()" style="padding:4px 8px; border:1px solid var(--border);
        border-radius:4px; background:var(--surface); color:var(--text); font-size:0.85rem;">
        <option value="">Alle</option>
        <option value="INFO">INFO</option>
        <option value="WARNING">WARNING</option>
        <option value="ERROR">ERROR</option>
      </select>
      <button class="btn btn-save" onclick="reloadLogs()" style="padding:4px 12px; font-size:0.8rem;">🔄 Aktualisieren</button>
      <label style="font-size:0.8rem; color:var(--muted); display:flex; align-items:center; gap:4px;">
        <input type="checkbox" id="log-auto" onchange="toggleAutoLog()"> Auto-Refresh
      </label>
    </div>
    <div class="log-box" id="log-viewer" style="height:60vh; max-height:700px;"></div>
  </div>
</div><!-- /tab-logs -->

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
  const names = {api: 'API Server', metadata: 'Metadata Server', proxy: 'Proxy', sync: 'STRM-Sync'};
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

    // Scrape Buttons sperren/freigeben
    const batchBtn = document.getElementById('btn-detail-batch');
    const singleBtn = document.getElementById('btn-detail-single');
    const incBtn = document.getElementById('btn-incremental');
    if (batchBtn) batchBtn.disabled = running;
    if (singleBtn) singleBtn.disabled = running;
    if (incBtn) incBtn.disabled = running;

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

  // Metadata Sync Status
  const metaDetail = data.metadata && data.metadata.detail;
  const metaBox = document.getElementById('meta-status');
  const metaBtn = document.getElementById('btn-meta-sync');

  if (metaDetail) {
    const running = metaDetail.syncRunning;
    const prog = metaDetail.syncProgress || {};
    const total = prog.total || 0;
    const done = prog.done || 0;
    const fetched = prog.fetched || 0;
    const skipped = prog.skipped || 0;
    const errors = prog.errors || 0;
    const metaTotal = metaDetail.total_metadata || 0;
    const coversCached = metaDetail.covers_cached || 0;

    // Button sperren/freigeben
    if (metaBtn) metaBtn.disabled = running;

    if (running && total > 0) {
      const pct = Math.round((done / total) * 100);
      metaBox.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <span>🔄 <strong>Sync läuft...</strong></span>
          <span style="color:var(--accent); font-weight:600;">${pct}%</span>
        </div>
        <div style="background:var(--border); border-radius:4px; height:8px; overflow:hidden;">
          <div style="background:var(--accent); height:100%; width:${pct}%; transition:width 0.5s;"></div>
        </div>
        <div style="margin-top:6px; font-size:0.8rem; color:var(--muted);">
          ${done} / ${total} — ${fetched} aktualisiert, ${skipped} übersprungen${errors > 0 ? `, ${errors} Fehler` : ''}
        </div>
      `;
    } else if (running) {
      metaBox.innerHTML = '<span>🔄 <strong>Sync läuft...</strong> Anime-Liste wird geladen...</span>';
    } else {
      metaBox.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span>✅ <strong>${metaTotal}</strong> Anime mit Metadata</span>
          <span style="font-size:0.8rem; color:var(--muted);">${coversCached} Cover gecacht</span>
        </div>
      `;
    }
  } else {
    if (metaBox) metaBox.innerHTML = '<span style="color:var(--muted);">Metadata Status: Server nicht erreichbar</span>';
    if (metaBtn) metaBtn.disabled = false;
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

async function metadataSync() {
  const btn = document.getElementById('btn-meta-sync');
  btn.disabled = true;
  document.getElementById('meta-result').textContent = 'Metadata Sync wird gestartet...';
  try {
    const r = await fetch(API + '/api/dashboard/metadata-sync', {method:'POST'});
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); btn.disabled = false; return; }
    toast('Metadata Sync gestartet!');
    document.getElementById('meta-result').innerHTML =
      '✅ AniList Metadata Sync gestartet';
    fetchStatus();
  } catch(e) { toast('Fehler: ' + e, false); btn.disabled = false; }
}

async function incrementalSync() {
  const btn = document.getElementById('btn-incremental');
  const resultEl = document.getElementById('incremental-result');
  btn.disabled = true;
  resultEl.textContent = 'Scrape läuft...';
  try {
    const r = await fetch(API + '/api/dashboard/incremental-sync', {method:'POST'});
    if (!r.ok) {
      if (r.status === 409) { toast('Incremental Sync läuft bereits!', false); return; }
      const e = await r.json(); toast(e.detail, false); return;
    }
    toast('Änderungen werden gescraped...');
    pollSyncStatus('incremental', resultEl, btn);
  } catch(e) { toast('Fehler: ' + e, false); btn.disabled = false; }
}

async function pollSyncStatus(mode, resultEl, btn) {
  const endpoint = mode === 'incremental' ? '/api/dashboard/incremental-sync/status' : '/api/dashboard/full-sync/status';
  const poll = setInterval(async () => {
    try {
      const r = await fetch(API + endpoint);
      const data = await r.json();
      if (data.running) {
        resultEl.textContent = '⏳ Scrape läuft...';
        return;
      }
      clearInterval(poll);
      btn.disabled = false;
      if (!data.result) { resultEl.textContent = ''; return; }
      const res = data.result;
      if (res.error) {
        resultEl.innerHTML = '❌ Fehler: ' + res.error;
        toast('Sync fehlgeschlagen!', false);
      } else {
        const parts = [];
        if (res.new_anime !== undefined) parts.push(res.new_anime + ' neue Anime');
        if (res.updated_anime !== undefined) parts.push(res.updated_anime + ' aktualisiert');
        if (res.errors !== undefined && res.errors > 0) parts.push(res.errors + ' Fehler');
        const hasErrors = res.errors > 0;
        const icon = hasErrors ? '⚠️' : '✅';
        resultEl.innerHTML = icon + ' Fertig: ' + parts.join(', ');
        toast(hasErrors ? 'Sync fertig (mit Fehlern)' : 'Sync erfolgreich!', !hasErrors);
      }
    } catch(e) { /* keep polling */ }
  }, 5000);
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

async function logout() {
  await fetch(API + '/api/auth/logout', {method:'POST'});
  window.location.href = '/login';
}

function renderPwSection() {
  const sec = document.getElementById('pw-section');
  // Check if auth is enabled by trying a quick test
  sec.innerHTML = `
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:end;">
      <div>
        <label style="font-size:0.8rem; color:var(--muted);">Aktuelles Passwort</label>
        <input type="password" id="pw-current" style="display:block; padding:6px 10px; border:1px solid var(--border);
          border-radius:4px; background:var(--bg); color:var(--text); font-size:0.85rem; width:180px; margin-top:4px;">
      </div>
      <div>
        <label style="font-size:0.8rem; color:var(--muted);">Neues Passwort</label>
        <input type="password" id="pw-new" style="display:block; padding:6px 10px; border:1px solid var(--border);
          border-radius:4px; background:var(--bg); color:var(--text); font-size:0.85rem; width:180px; margin-top:4px;">
      </div>
      <button class="btn btn-save" onclick="changePw()" style="height:34px;">Ändern</button>
    </div>
    <div id="pw-result" style="margin-top:8px; font-size:0.8rem; color:var(--muted);"></div>
  `;
}

async function changePw() {
  const current = document.getElementById('pw-current').value;
  const newPw = document.getElementById('pw-new').value;
  if (!newPw || newPw.length < 4) { toast('Passwort muss mind. 4 Zeichen haben', false); return; }
  try {
    const r = await fetch(API + '/api/auth/change-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({current, "new": newPw})
    });
    if (r.ok) {
      toast('Passwort geändert!');
      document.getElementById('pw-current').value = '';
      document.getElementById('pw-new').value = '';
    } else {
      const e = await r.json();
      toast(e.detail || 'Fehler', false);
    }
  } catch(e) { toast('Fehler: ' + e, false); }
}

// === Tab Navigation ===
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  ['dashboard','recent','catalog','config','crons','logs','settings'].forEach(t => document.getElementById('tab-'+t).style.display = t===tab?'':'none');
  event.target.classList.add('active');
  if (tab === 'recent' && !document.getElementById('recent-list').innerHTML) loadRecentChanges();
  if (tab === 'catalog' && !document.getElementById('catalog-letters').innerHTML) loadLetters();
  if (tab === 'config' && !document.getElementById('config-editor').value) loadConfig();
  if (tab === 'crons' && !document.getElementById('crons-list').innerHTML) loadCrons();
  if (tab === 'logs' && !document.getElementById('log-viewer').innerHTML) loadLogs('api');
}

// === Katalog ===
let currentLetter = '';

async function loadLetters() {
  try {
    const r = await fetch(API + '/api/dashboard/catalog/letters');
    const data = await r.json();
    const el = document.getElementById('catalog-letters');
    let total = 0;
    el.innerHTML = data.map(l => {
      total += l.cnt || l.count || 0;
      const cnt = l.cnt || l.count || 0;
      return `<button class="letter-btn" onclick="loadByLetter('${l.letter}')" title="${cnt} Anime">${l.letter}</button>`;
    }).join('');
    document.getElementById('catalog-info').textContent = `${total} Anime im Katalog`;
  } catch(e) { console.error(e); }
}

async function loadByLetter(letter) {
  currentLetter = letter;
  document.querySelectorAll('.letter-btn').forEach(b => b.classList.toggle('active', b.textContent === letter));
  document.getElementById('catalog-detail').style.display = 'none';
  document.getElementById('catalog-list').style.display = '';
  document.getElementById('catalog-info').textContent = 'Lade...';
  try {
    const r = await fetch(API + '/api/dashboard/catalog/anime?letter=' + encodeURIComponent(letter));
    const data = await r.json();
    renderAnimeList(data, `${data.length} Anime mit "${letter}"`);
  } catch(e) { toast('Fehler: ' + e, false); }
}

async function catalogSearch() {
  const q = document.getElementById('catalog-search').value.trim();
  if (!q) return;
  document.querySelectorAll('.letter-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('catalog-detail').style.display = 'none';
  document.getElementById('catalog-list').style.display = '';
  document.getElementById('catalog-info').textContent = 'Suche...';
  try {
    const r = await fetch(API + '/api/dashboard/catalog/anime?q=' + encodeURIComponent(q));
    const data = await r.json();
    renderAnimeList(data, `${data.length} Ergebnis${data.length !== 1 ? 'se' : ''} für "${q}"`);
  } catch(e) { toast('Fehler: ' + e, false); }
}

function renderAnimeList(anime, info) {
  document.getElementById('catalog-info').textContent = info;
  const el = document.getElementById('catalog-list');
  if (!anime || anime.length === 0) {
    el.innerHTML = '<div style="color:var(--muted);">Keine Anime gefunden.</div>';
    return;
  }
  el.innerHTML = anime.map(a => `
    <div class="anime-card" onclick="loadAnimeDetail('${a.slug}')">
      <h3>${a.title}</h3>
      <div class="meta">
        ${a.slug}
        ${a.season_count ? ` — ${a.season_count} Staffel${a.season_count > 1 ? 'n' : ''}` : ''}
        ${a.has_movies ? ' — 🎬 Filme' : ''}
        ${a.last_scraped ? ' — ✅ Details' : ' — ⏳ Pending'}
      </div>
    </div>
  `).join('');
}

async function loadAnimeDetail(slug) {
  document.getElementById('catalog-list').style.display = 'none';
  const detail = document.getElementById('catalog-detail');
  detail.style.display = '';
  detail.innerHTML = '<div style="color:var(--muted);">Lade Details...</div>';
  try {
    const r = await fetch(API + '/api/dashboard/catalog/anime/' + encodeURIComponent(slug));
    const a = await r.json();
    let html = `
      <div class="back-btn" onclick="backToList()">← Zurück zur Liste</div>
      <div style="display:flex; gap:16px; flex-wrap:wrap; margin-bottom:16px;">
        ${a.cover_url ? `<img src="${a.cover_url}" style="width:120px; border-radius:8px;" onerror="this.style.display='none'">` : ''}
        <div style="flex:1; min-width:200px;">
          <h2 style="margin-bottom:8px;">${a.title}</h2>
          <div style="font-size:0.85rem; color:var(--muted); margin-bottom:8px;">
            Slug: ${a.slug}<br>
            ${a.season_count ? `Staffeln: ${a.season_count}` : ''}
            ${a.has_movies ? ' | Filme: Ja' : ''}
          </div>
          ${a.description ? `<p style="font-size:0.85rem; line-height:1.5; max-height:120px; overflow-y:auto;">${a.description}</p>` : ''}
        </div>
      </div>
    `;

    // Season buttons
    if (a.season_count > 0) {
      html += '<div style="margin-bottom:12px;">';
      for (let i = 1; i <= a.season_count; i++) {
        html += `<button class="letter-btn" onclick="loadEpisodes('${slug}', ${i})" style="margin:2px;">Staffel ${i}</button>`;
      }
      if (a.has_movies) {
        html += `<button class="letter-btn" onclick="loadEpisodes('${slug}', 0)" style="margin:2px;">🎬 Filme</button>`;
      }
      html += '</div>';
    }
    html += '<div id="episode-list"></div>';
    detail.innerHTML = html;
  } catch(e) { detail.innerHTML = '<div style="color:var(--red);">Fehler beim Laden.</div>'; }
}

async function loadEpisodes(slug, season) {
  const el = document.getElementById('episode-list');
  el.innerHTML = '<div style="color:var(--muted);">Lade Episoden...</div>';
  try {
    const url = season === 0
      ? API + '/api/dashboard/catalog/anime/' + slug + '/films/episodes'
      : API + '/api/dashboard/catalog/anime/' + slug + '/season/' + season + '/episodes';
    const r = await fetch(url);
    const eps = await r.json();
    if (!eps || eps.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);">Keine Episoden gefunden. Noch nicht gescraped?</div>';
      return;
    }
    el.innerHTML = `
      <div style="background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden;">
        ${eps.map(ep => `
          <div class="episode-row">
            <strong>E${ep.episode_number || ep.number || '?'}</strong>
            ${ep.title ? ` — ${ep.title}` : ''}
            ${ep.title_en ? ` <span style="color:var(--muted);">(${ep.title_en})</span>` : ''}
          </div>
        `).join('')}
      </div>
    `;
  } catch(e) { el.innerHTML = '<div style="color:var(--red);">Fehler beim Laden.</div>'; }
}

function backToList() {
  document.getElementById('catalog-detail').style.display = 'none';
  document.getElementById('catalog-list').style.display = '';
}

// === Log Viewer ===
let currentLogService = 'api';
let logAutoInterval = null;

async function loadLogs(service, btn) {
  currentLogService = service;
  if (btn) {
    document.querySelectorAll('#tab-logs .letter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  const level = document.getElementById('log-level').value;
  const viewer = document.getElementById('log-viewer');
  try {
    const r = await fetch(API + '/api/dashboard/logs/' + service + '?lines=200' + (level ? '&level=' + level : ''));
    const data = await r.json();
    viewer.innerHTML = data.lines.map(line => {
      let cls = '';
      if (line.includes('ERROR')) cls = 'error';
      else if (line.includes('WARNING')) cls = 'warn';
      else if (line.includes('INFO')) cls = 'info';
      return `<div class="${cls}">${line.replace(/</g,'&lt;')}</div>`;
    }).join('');
    viewer.scrollTop = viewer.scrollHeight;
  } catch(e) { viewer.innerHTML = '<div class="error">Fehler beim Laden: ' + e + '</div>'; }
}

function reloadLogs() { loadLogs(currentLogService); }

function toggleAutoLog() {
  if (document.getElementById('log-auto').checked) {
    logAutoInterval = setInterval(reloadLogs, 3000);
  } else {
    clearInterval(logAutoInterval);
    logAutoInterval = null;
  }
}

// === Crons ===
let cronsData = {};

function describeCron(expr) {
  try {
    const p = expr.trim().split(/\s+/);
    if (p.length !== 5) return '(ungültig)';
    const [min, hour, dom, mon, dow] = p;
    const dowNames = ['So','Mo','Di','Mi','Do','Fr','Sa'];

    // Every minute
    if (min === '*' && hour === '*' && dom === '*' && mon === '*' && dow === '*') return '(jede Minute)';

    // */N minutes
    if (min.startsWith('*/') && hour === '*' && dom === '*') {
      return `(alle ${min.slice(2)} Min.)`;
    }

    // Every N hours
    if (min !== '*' && hour.startsWith('*/') && dom === '*') {
      return `(alle ${hour.slice(2)}h um :${min.padStart(2,'0')})`;
    }

    // Specific hour(s)
    if (min !== '*' && !hour.includes('*') && !hour.includes('/') && dom === '*' && mon === '*') {
      const time = `${hour.padStart(2,'0')}:${min.padStart(2,'0')}`;
      if (dow === '*') return `(täglich ${time})`;
      // Specific weekday(s)
      const days = dow.split(',').map(d => dowNames[parseInt(d)] || d).join(', ');
      return `(${days} ${time})`;
    }

    // Specific dom
    if (min !== '*' && hour !== '*' && dom !== '*' && dom !== '*/') {
      return `(Tag ${dom}, ${hour.padStart(2,'0')}:${min.padStart(2,'0')})`;
    }

    return '(benutzerdefiniert)';
  } catch(e) { return '(ungültig)'; }
}

function updateCronHint(id) {
  const input = document.getElementById('cron-schedule-' + id);
  const hint = document.getElementById('cron-hint-' + id);
  if (input && hint) hint.textContent = describeCron(input.value);
}

async function loadCrons() {
  try {
    const r = await fetch(API + '/api/dashboard/crons');
    cronsData = await r.json();
    renderCrons();
  } catch(e) { toast('Crons laden fehlgeschlagen', false); }
}

function renderCrons() {
  const el = document.getElementById('crons-list');
  let html = '';
  for (const [id, job] of Object.entries(cronsData)) {
    const lastRun = job.last_run ? new Date(job.last_run).toLocaleString('de-DE') : 'Noch nie';
    html += `
      <div style="background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:10px;">
        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
          <label style="display:flex; align-items:center; gap:8px; min-width:200px; cursor:pointer;">
            <input type="checkbox" id="cron-enabled-${id}" ${job.enabled ? 'checked' : ''}
              onchange="cronsData['${id}'].enabled = this.checked"
              style="width:18px; height:18px; accent-color:var(--accent);">
            <strong style="font-size:0.95rem;">${job.name}</strong>
          </label>
          <input type="text" id="cron-schedule-${id}" value="${job.schedule}" placeholder="* * * * *"
            onchange="cronsData['${id}'].schedule = this.value; updateCronHint('${id}')"
            oninput="updateCronHint('${id}')"
            style="padding:6px 10px; border:1px solid var(--border); border-radius:4px;
              background:var(--bg); color:var(--text); font-family:monospace; font-size:0.9rem; width:160px;">
          <span id="cron-hint-${id}" style="color:var(--muted); font-size:0.8rem; min-width:120px;">${describeCron(job.schedule)}</span>
          <button class="btn btn-start" onclick="cronRunNow('${id}')" style="padding:4px 12px; font-size:0.8rem;">
            ▶ Jetzt
          </button>
          <span style="color:var(--muted); font-size:0.8rem;">Letzter Lauf: ${lastRun}</span>
        </div>
      </div>`;
  }
  el.innerHTML = html;
}

async function cronsSave() {
  // Werte aus Inputs lesen
  for (const id of Object.keys(cronsData)) {
    const schedEl = document.getElementById('cron-schedule-' + id);
    const enEl = document.getElementById('cron-enabled-' + id);
    if (schedEl) cronsData[id].schedule = schedEl.value;
    if (enEl) cronsData[id].enabled = enEl.checked;
  }
  try {
    const r = await fetch(API + '/api/dashboard/crons', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cronsData)
    });
    if (!r.ok) { const e = await r.json(); toast(e.detail, false); return; }
    toast('Cronjobs gespeichert!');
    document.getElementById('crons-result').textContent = '✅ Gespeichert';
    setTimeout(() => document.getElementById('crons-result').textContent = '', 3000);
  } catch(e) { toast('Fehler: ' + e, false); }
}

async function cronRunNow(jobId) {
  try {
    const r = await fetch(API + '/api/dashboard/crons/' + jobId + '/run', {method:'POST'});
    if (r.ok) {
      toast('Job gestartet!');
      setTimeout(loadCrons, 2000);
    } else {
      const e = await r.json();
      toast(e.detail, false);
    }
  } catch(e) { toast('Fehler: ' + e, false); }
}

// === Recent Changes ===
async function loadRecentChanges() {
  const days = document.getElementById('recent-days').value;
  const el = document.getElementById('recent-list');
  el.innerHTML = '<span style="color:var(--muted)">Lade...</span>';
  try {
    const r = await fetch(API + '/api/dashboard/recent-changes?days=' + days + '&limit=200');
    const data = await r.json();
    if (!data || data.length === 0) {
      el.innerHTML = '<div style="color:var(--muted); padding:20px; text-align:center;">Keine Änderungen im gewählten Zeitraum.</div>';
      return;
    }
    // Group by date
    const byDate = {};
    data.forEach(c => {
      const d = c.createdAt ? c.createdAt.split('T')[0] : 'Unbekannt';
      if (!byDate[d]) byDate[d] = [];
      byDate[d].push(c);
    });
    const icons = {new_anime:'🆕', new_season:'📺', new_episodes:'🎬', new_films:'🎥'};
    const labels = {new_anime:'Neuer Anime', new_season:'Neue Staffel', new_episodes:'Neue Episoden', new_films:'Neue Filme'};
    let html = '';
    for (const [date, changes] of Object.entries(byDate)) {
      html += '<div style="margin-bottom:20px;">';
      html += '<h3 style="color:var(--accent); margin:0 0 8px 0; font-size:0.95rem;">' + date + '</h3>';
      changes.forEach(c => {
        const icon = icons[c.changeType] || '📌';
        const label = labels[c.changeType] || c.changeType;
        html += '<div style="padding:6px 12px; margin:4px 0; background:var(--card); border-radius:6px; border-left:3px solid var(--accent);">';
        html += icon + ' <strong>' + (c.title || c.slug) + '</strong>';
        html += ' <span style="color:var(--muted); margin-left:8px;">' + (c.detail || label) + '</span>';
        html += '</div>';
      });
      html += '</div>';
    }
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<span style="color:var(--red)">Fehler: ' + e + '</span>';
  }
}

// Init
fetchStatus();
renderPwSection();
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
