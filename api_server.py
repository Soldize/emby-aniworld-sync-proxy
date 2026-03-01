import os
import sqlite3
import threading
import time
import logging
import re
import base64
from datetime import datetime, timedelta, timezone


def _parse_dt(s):
    """Parse ISO datetime string, assume UTC if naive."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
from html import unescape

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, g

try:
    import schedule
except ImportError:
    schedule = None
    logging.warning("schedule module not found - nightly scrape will be disabled")

app = Flask(__name__)

import configparser as _cp
_cfg = _cp.ConfigParser()
_cfg.read(os.environ.get("ANIWORLD_CONFIG", "/etc/aniworld/config.ini"))

DB_PATH = _cfg.get("api", "db_path", fallback="/opt/aniworld/data/aniworld.db")
METADATA_PORT = _cfg.get("metadata", "port", fallback="5090")
METADATA_BASE = f"http://127.0.0.1:{METADATA_PORT}"
BASE_URL = "https://aniworld.to"
SYNC_INTERVAL = 300  # 5 minutes between background sync batches
DETAIL_SCRAPE_DELAY = 3  # seconds between requests to aniworld.to (catalog scraping)
STREAM_SCRAPE_DELAY = 0.25  # seconds between requests for stream resolving (faster)
INCREMENTAL_CHECK_DELAY = 1.0  # faster delay for incremental sync checks
DETAIL_CACHE_DAYS = 7  # don't re-scrape details within this period
DETAIL_BATCH_SIZE = 100  # how many details to scrape per background run

# Global rate limiter - only one aniworld.to request at a time
_scrape_lock = threading.Lock()
_last_request_time = 0.0
_detail_sync_running = False


logging.Formatter.converter = time.localtime
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aniworld-api")

# SOCKS5 proxy support (e.g. Cloudflare WARP in proxy mode)
# WARP is only used for hoster stream resolution (VOE, Vidmoly etc.)
# aniworld.to scraping goes direct - no proxy needed, avoids WARP instability
WARP_PROXY = os.environ.get("WARP_PROXY", "").strip()
if not WARP_PROXY:
    WARP_PROXY = _cfg.get("proxy", "warp_socks5", fallback="").strip()
if WARP_PROXY:
    logging.info(f"WARP SOCKS5 proxy enabled (hoster-only): {WARP_PROXY}")
    _HOSTER_PROXIES = {"http": WARP_PROXY, "https": WARP_PROXY}
else:
    _HOSTER_PROXIES = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}

def _rate_limited_get(url, timeout=30, max_wait=10, delay=None):
    """All requests to aniworld.to go through here - enforces delay between requests.
    max_wait: max seconds to wait for the lock before making the request anyway.
    delay: override the default DETAIL_SCRAPE_DELAY (e.g. STREAM_SCRAPE_DELAY)."""
    global _last_request_time
    effective_delay = delay if delay is not None else DETAIL_SCRAPE_DELAY
    acquired = _scrape_lock.acquire(timeout=max_wait)
    try:
        elapsed = time.time() - _last_request_time
        if elapsed < effective_delay:
            time.sleep(effective_delay - elapsed)
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        _last_request_time = time.time()
        return resp
    finally:
        if acquired:
            _scrape_lock.release()

# --- DB ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db

def get_conn():
    """Get a standalone connection (for background threads)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS anime (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            letter TEXT NOT NULL,
            has_movies INTEGER DEFAULT 0,
            description TEXT,
            cover_url TEXT,
            season_count INTEGER DEFAULT 0,
            last_scraped TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_anime_letter ON anime(letter);

        CREATE TABLE IF NOT EXISTS season (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            episode_count INTEGER DEFAULT 0,
            FOREIGN KEY (anime_slug) REFERENCES anime(slug),
            UNIQUE(anime_slug, season_number)
        );

        CREATE TABLE IF NOT EXISTS episode (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT NOT NULL,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            title TEXT,
            title_en TEXT,
            url TEXT NOT NULL,
            last_scraped TEXT,
            FOREIGN KEY (anime_slug) REFERENCES anime(slug),
            UNIQUE(anime_slug, season_number, episode_number)
        );
        CREATE INDEX IF NOT EXISTS idx_episode_lookup ON episode(anime_slug, season_number);

        CREATE TABLE IF NOT EXISTS recent_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            title TEXT,
            change_type TEXT NOT NULL,  -- 'new_anime', 'new_season', 'new_episodes', 'new_films'
            detail TEXT,  -- e.g. "S03: 3 neue Episoden" or "Staffel 4 hinzugefügt"
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_recent_changes_date ON recent_changes(created_at);

        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            entries_total INTEGER DEFAULT 0,
            entries_updated INTEGER DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS stream_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            hoster TEXT,
            language TEXT,
            lang_key INTEGER,
            redirect_url TEXT,
            redirect_cached_at TEXT,
            stream_url TEXT,
            stream_cached_at TEXT,
            failed_at TEXT,
            last_accessed TEXT,
            cached_at TEXT,
            UNIQUE(slug, season, episode, hoster, lang_key)
        );
        CREATE INDEX IF NOT EXISTS idx_stream_lookup ON stream_cache(slug, season, episode);
    """)

    # Migrate: add missing columns to stream_cache (for existing DBs)
    try:
        cur = conn.execute("PRAGMA table_info(stream_cache)")
        cols = {row[1] for row in cur.fetchall()}
        migrations = {
            "redirect_cached_at": "ALTER TABLE stream_cache ADD COLUMN redirect_cached_at TEXT",
            "stream_url": "ALTER TABLE stream_cache ADD COLUMN stream_url TEXT",
            "stream_cached_at": "ALTER TABLE stream_cache ADD COLUMN stream_cached_at TEXT",
            "failed_at": "ALTER TABLE stream_cache ADD COLUMN failed_at TEXT",
            "last_accessed": "ALTER TABLE stream_cache ADD COLUMN last_accessed TEXT",
        }
        for col, sql in migrations.items():
            if col not in cols:
                conn.execute(sql)
                log.info(f"Migrated stream_cache: added column {col}")
        conn.commit()
    except Exception as e:
        log.warning(f"stream_cache migration check failed: {e}")

    conn.close()

# --- Episode Scraper ---

def scrape_season_episodes(slug, season_number):
    """Scrape episode list for a specific season. Returns episode count."""
    log.info(f"Scraping episodes for {slug} season {season_number}")
    
    try:
        url = f"{BASE_URL}/anime/stream/{slug}/staffel-{season_number}"
        resp = _rate_limited_get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        episodes = []
        episode_rows = soup.select("table.seasonEpisodesList tbody tr[data-episode-id]") or soup.select("tr[data-episode-id]")
        
        if episode_rows:
            for row in episode_rows:
                ep_meta = row.select_one("meta[itemprop='episodeNumber']")
                ep_num_str = ep_meta.get("content", "0") if ep_meta else "0"
                ep_num = int(ep_num_str) if ep_num_str.isdigit() else 0
                if ep_num == 0:
                    continue
                
                title_node = row.select_one("td.seasonEpisodeTitle strong")
                ep_title = unescape(title_node.get_text(strip=True)) if title_node else ""
                if not ep_title:
                    span_node = row.select_one("td.seasonEpisodeTitle span")
                    ep_title = unescape(span_node.get_text(strip=True)) if span_node else ""
                
                link_node = row.select_one("a[itemprop='url']") or row.select_one("a[href*='/episode-']")
                href = link_node.get("href", "") if link_node else ""
                if not href:
                    continue
                ep_url = href if href.startswith("http") else BASE_URL + href
                
                episodes.append((ep_num, ep_title, ep_url))
        else:
            # Fallback: find episode links
            episode_links = soup.select("a[href*='/episode-']")
            seen = set()
            for link in episode_links:
                href = link.get("href", "")
                m = re.search(r"episode-(\d+)", href)
                if not m:
                    continue
                ep_num = int(m.group(1))
                if ep_num in seen:
                    continue
                seen.add(ep_num)
                
                ep_title = unescape(link.get_text(strip=True))
                ep_url = href if href.startswith("http") else BASE_URL + href
                episodes.append((ep_num, ep_title, ep_url))
        
        # Write to DB
        conn = get_conn()
        try:
            timestamp = datetime.now(tz=timezone.utc).isoformat()
            for ep_num, title, url in episodes:
                conn.execute("""
                    INSERT INTO episode (anime_slug, season_number, episode_number, title, url, last_scraped)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(anime_slug, season_number, episode_number) DO UPDATE SET
                        title=excluded.title, url=excluded.url, last_scraped=excluded.last_scraped
                """, (slug, season_number, ep_num, title, url, timestamp))
            
            # Update episode count in season table
            conn.execute("""
                UPDATE season SET episode_count=? 
                WHERE anime_slug=? AND season_number=?
            """, (len(episodes), slug, season_number))
            conn.commit()
        finally:
            conn.close()
        
        log.info(f"Scraped {len(episodes)} episodes for {slug} S{season_number}")
        return len(episodes)
    
    except Exception as e:
        log.error(f"Failed to scrape episodes for {slug} S{season_number}: {e}")
        return 0

def scrape_film_episodes(slug):
    """Scrape film/movie list (season_number=0). Returns episode count."""
    log.info(f"Scraping films for {slug}")
    
    try:
        url = f"{BASE_URL}/anime/stream/{slug}/filme"
        resp = _rate_limited_get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        episodes = []
        episode_rows = soup.select("table.seasonEpisodesList tbody tr[data-episode-id]") or soup.select("tr[data-episode-id]")
        
        if episode_rows:
            for row in episode_rows:
                # Try meta tag first, then parse from link href
                ep_meta = row.select_one("meta[itemprop='episodeNumber']")
                ep_num = 0
                if ep_meta:
                    ep_num_str = ep_meta.get("content", "0")
                    ep_num = int(ep_num_str) if ep_num_str.isdigit() else 0
                
                title_node = row.select_one("td.seasonEpisodeTitle strong")
                ep_title = unescape(title_node.get_text(strip=True)) if title_node else ""
                if not ep_title:
                    span_node = row.select_one("td.seasonEpisodeTitle span")
                    ep_title = unescape(span_node.get_text(strip=True)) if span_node else ""
                
                link_node = (row.select_one("a[itemprop='url']") or 
                            row.select_one("a[href*='/film-']") or 
                            row.select_one("a[href*='/episode-']"))
                href = link_node.get("href", "") if link_node else ""
                if not href:
                    continue
                
                # Parse episode number from href if meta tag was missing
                if ep_num == 0:
                    m = re.search(r"(?:film|episode)-(\d+)", href)
                    if m:
                        ep_num = int(m.group(1))
                if ep_num == 0:
                    continue
                ep_url = href if href.startswith("http") else BASE_URL + href
                
                episodes.append((ep_num, ep_title, ep_url))
        else:
            # Fallback
            film_links = soup.select("a[href*='/film-'], a[href*='/episode-']")
            seen = set()
            counter = 1
            for link in film_links:
                href = link.get("href", "")
                m = re.search(r"(?:film|episode)-(\d+)", href)
                ep_num = int(m.group(1)) if m else counter
                if ep_num in seen:
                    continue
                seen.add(ep_num)
                
                ep_title = unescape(link.get_text(strip=True))
                ep_url = href if href.startswith("http") else BASE_URL + href
                episodes.append((ep_num, ep_title, ep_url))
                counter += 1
        
        # Write to DB
        conn = get_conn()
        try:
            timestamp = datetime.now(tz=timezone.utc).isoformat()
            for ep_num, title, url in episodes:
                conn.execute("""
                    INSERT INTO episode (anime_slug, season_number, episode_number, title, url, last_scraped)
                    VALUES (?, 0, ?, ?, ?, ?)
                    ON CONFLICT(anime_slug, season_number, episode_number) DO UPDATE SET
                        title=excluded.title, url=excluded.url, last_scraped=excluded.last_scraped
                """, (slug, ep_num, title, url, timestamp))
            conn.commit()
        finally:
            conn.close()
        
        log.info(f"Scraped {len(episodes)} films for {slug}")
        return len(episodes)
    
    except Exception as e:
        log.error(f"Failed to scrape films for {slug}: {e}")
        return 0

HOSTER_CACHE_TTL_DAYS = 7   # redirect_url: stable, changes rarely
HOSTER_FAIL_TTL_MINUTES = int(_cfg.get("api", "fail_ttl_minutes", fallback="30"))  # wie lange ein fehlgeschlagener Hoster übersprungen wird
STREAM_URL_CACHE_TTL_H = 2  # stream_url (CDN): expires ~2h

def _get_hoster_cache(slug, season, episode):
    """Get cached hoster list. Returns list of dicts with redirectUrl (and streamUrl if fresh)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT hoster, language, lang_key, redirect_url, stream_url, redirect_cached_at, stream_cached_at, failed_at FROM stream_cache WHERE slug=? AND season=? AND episode=?",
            (slug, season, episode)
        ).fetchall()
        if not rows:
            return None
        # Check redirect TTL (7 days)
        rca = _parse_dt(rows[0]["redirect_cached_at"])
        if datetime.now(tz=timezone.utc) - rca > timedelta(days=HOSTER_CACHE_TTL_DAYS):
            return None
        result = []
        for r in rows:
            h = {"name": r["hoster"], "language": r["language"], "langKey": r["lang_key"], "redirectUrl": r["redirect_url"], "streamUrl": None, "failedAt": r["failed_at"]}
            # Check CDN URL TTL (2h)
            if r["stream_url"] and r["stream_cached_at"]:
                sca = _parse_dt(r["stream_cached_at"])
                if datetime.now(tz=timezone.utc) - sca <= timedelta(hours=STREAM_URL_CACHE_TTL_H):
                    h["streamUrl"] = r["stream_url"]
            result.append(h)
        return result
    finally:
        conn.close()

def _set_hoster_cache(slug, season, episode, hosters):
    """Save hoster list (redirect URLs only) to DB."""
    conn = get_conn()
    try:
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute("DELETE FROM stream_cache WHERE slug=? AND season=? AND episode=?", (slug, season, episode))
        for h in hosters:
            conn.execute(
                "INSERT INTO stream_cache (slug, season, episode, hoster, language, lang_key, redirect_url, redirect_cached_at, cached_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (slug, season, episode, h.get("name"), h.get("language"), h.get("langKey"), h["redirectUrl"], now, now)
            )
        conn.commit()
    finally:
        conn.close()

def _update_stream_url_cache(slug, season, episode, hoster_name, lang_key, stream_url):
    """Update CDN stream URL in cache for a specific hoster."""
    conn = get_conn()
    try:
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE stream_cache SET stream_url=?, stream_cached_at=?, last_accessed=? WHERE slug=? AND season=? AND episode=? AND hoster=? AND lang_key=?",
            (stream_url, now, now, slug, season, episode, hoster_name, lang_key)
        )
        conn.commit()
    finally:
        conn.close()

def _mark_hoster_failed(slug, season, episode, hoster_name, lang_key):
    """Mark a hoster as failed so we skip it for 6h."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE stream_cache SET failed_at=? WHERE slug=? AND season=? AND episode=? AND hoster=? AND lang_key=?",
            (datetime.now(tz=timezone.utc).isoformat(), slug, season, episode, hoster_name, lang_key)
        )
        conn.commit()
    finally:
        conn.close()

def _scrape_hoster_list(slug, season, episode):
    """Fetch episode page from aniworld.to and cache hoster redirect URLs.
    Only 1 HTTP request. Returns list of {name, language, langKey, redirectUrl}."""
    try:
        if season == 0:
            url = f"{BASE_URL}/anime/stream/{slug}/filme/film-{episode}"
        else:
            url = f"{BASE_URL}/anime/stream/{slug}/staffel-{season}/episode-{episode}"

        resp = _rate_limited_get(url, delay=STREAM_SCRAPE_DELAY)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        hoster_nodes = soup.select("div.hosterSiteVideo li[data-link-target]")
        if not hoster_nodes:
            log.warn(f"No hosters found for {slug} S{season}E{episode}")
            return []

        hosters = []
        for node in hoster_nodes:
            redirect_url = node.get("data-link-target", "")
            if not redirect_url:
                continue
            h4 = node.select_one("h4")
            hoster_name = unescape(h4.get_text(strip=True)) if h4 else "Unknown"
            lang_key_str = node.get("data-lang-key", "0")
            lang_key = int(lang_key_str) if lang_key_str.isdigit() else 0
            language = {1: "Deutsch", 2: "EngSub", 3: "GerSub"}.get(lang_key, "Unknown")
            if not redirect_url.startswith("http"):
                redirect_url = BASE_URL + redirect_url
            hosters.append({"name": hoster_name, "language": language, "langKey": lang_key, "redirectUrl": redirect_url})

        if hosters:
            _set_hoster_cache(slug, season, episode, hosters)
            log.info(f"Cached hoster list for {slug} S{season}E{episode}: {len(hosters)} hosters")
        return hosters

    except Exception as e:
        log.error(f"Failed to scrape hoster list for {slug} S{season}E{episode}: {e}")
        return []

def _resolve_redirect_to_stream(hoster):
    """Follow one redirect URL to get the fresh CDN stream URL.
    Always called fresh - CDN URLs expire after 2-6h so we never cache them."""
    try:
        redirect_resp = _rate_limited_get(hoster["redirectUrl"], delay=STREAM_SCRAPE_DELAY)
        stream_url = extract_video_url(redirect_resp.url, redirect_resp.text)
        if stream_url:
            return {**hoster, "streamUrl": stream_url}
        return None
    except Exception as e:
        log.warn(f"Failed to resolve hoster {hoster['name']}: {e}")
        return None

def resolve_stream_urls(slug, season, episode):
    """Three-tier resolve:
    1. CDN URL in cache + fresh (<2h) → instant, no requests
    2. Hoster list cached, CDN URL stale/missing → 1 request per hoster (follow redirect)
    3. Nothing cached → scrape episode page (1 req) + follow redirect (1 req per hoster)
    """
    # Step 1: get hoster list (with any cached CDN URLs)
    hosters = _get_hoster_cache(slug, season, episode)
    if hosters is None:
        log.info(f"Full cache MISS for {slug} S{season}E{episode}, scraping episode page...")
        hosters = _scrape_hoster_list(slug, season, episode)
        if not hosters:
            return []
    else:
        log.info(f"Hoster cache HIT for {slug} S{season}E{episode}: {len(hosters)} hosters")
        # Background refresh of hoster list if approaching 7-day expiry
        conn = get_conn()
        try:
            row = conn.execute("SELECT redirect_cached_at FROM stream_cache WHERE slug=? AND season=? AND episode=? LIMIT 1", (slug, season, episode)).fetchone()
            if row and datetime.now(tz=timezone.utc) - _parse_dt(row["redirect_cached_at"]) > timedelta(days=6):
                threading.Thread(target=_scrape_hoster_list, args=(slug, season, episode), daemon=True).start()
        finally:
            conn.close()

    # Hoster preference order - only try these, in this order
    HOSTER_PRIORITY = ["VOE", "Vidmoly"]
    # Language priority: Deutsch(1) > GerSub(3) > EngSub(2)
    LANG_PRIORITY = [1, 3, 2]

    results_by_lang = {}  # langKey → result
    results_lock = threading.Lock()

    # Pass 1: return all cached CDN URLs instantly (no requests needed)
    for h in hosters:
        if h.get("streamUrl") and h["name"] in HOSTER_PRIORITY:
            lk = h["langKey"]
            if lk not in results_by_lang or HOSTER_PRIORITY.index(h["name"]) < HOSTER_PRIORITY.index(results_by_lang[lk]["name"]):
                results_by_lang[lk] = h
                log.info(f"CDN cache HIT: {h['name']} ({h['language']}) → instant")

    # Collect languages that still need resolving
    langs_to_resolve = [lk for lk in LANG_PRIORITY if lk not in results_by_lang]

    if not langs_to_resolve:
        results = [{"name": r["name"], "language": r["language"], "langKey": r["langKey"], "streamUrl": r["streamUrl"]} for r in results_by_lang.values()]
        log.info(f"Resolved {len(results)} streams for {slug} S{season}E{episode} (all cached)")
        return results

    def resolve_lang(lk):
        """Try hosters for one language key in priority order. Runs in parallel per language."""
        for hoster_name in HOSTER_PRIORITY:
            h = next((x for x in hosters if x["langKey"] == lk and x["name"] == hoster_name), None)
            if h is None:
                continue
            # Skip recently failed
            if h.get("failedAt"):
                try:
                    if datetime.now(tz=timezone.utc) - _parse_dt(h["failedAt"]) < timedelta(minutes=HOSTER_FAIL_TTL_MINUTES):
                        continue
                except Exception:
                    pass
            log.info(f"CDN cache MISS: trying {hoster_name} ({h['language']})...")
            resolved = _resolve_redirect_to_stream(h)
            if resolved:
                _update_stream_url_cache(slug, season, episode, hoster_name, lk, resolved["streamUrl"])
                with results_lock:
                    results_by_lang[lk] = resolved
                return  # Got one for this language, done
            else:
                _mark_hoster_failed(slug, season, episode, hoster_name, lk)

    # Pass 2: resolve all missing languages in parallel (one thread per language)
    threads = [threading.Thread(target=resolve_lang, args=(lk,), daemon=True) for lk in langs_to_resolve]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)  # max 30s per language

    results = [{"name": r["name"], "language": r["language"], "langKey": r["langKey"], "streamUrl": r["streamUrl"]} for r in results_by_lang.values()]
    log.info(f"Resolved {len(results)} streams for {slug} S{season}E{episode}")
    return results

def _trigger_emby_library_scan():
    """Trigger Emby Library Scan via API (if configured)."""
    emby_url = _cfg.get("emby", "url", fallback=None)
    emby_key = _cfg.get("emby", "api_key", fallback=None)
    if not emby_url or not emby_key:
        log.info("Emby Library Scan: skipped (no [emby] config)")
        return
    try:
        resp = requests.post(
            f"{emby_url}/emby/Library/Refresh",
            headers={"X-Emby-Token": emby_key},
            timeout=10
        )
        if resp.ok:
            log.info("Emby Library Scan triggered successfully")
        else:
            log.warning(f"Emby Library Scan failed: HTTP {resp.status_code}")
    except Exception as e:
        log.warning(f"Emby Library Scan failed: {e}")


def _log_change(slug, title, change_type, detail):
    """Log a change to the recent_changes table."""
    try:
        conn = get_conn()
        if not title:
            row = conn.execute("SELECT title FROM anime WHERE slug=?", (slug,)).fetchone()
            title = row["title"] if row else slug
        conn.execute(
            "INSERT INTO recent_changes (slug, title, change_type, detail) VALUES (?,?,?,?)",
            (slug, title, change_type, detail)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"Failed to log change for {slug}: {e}")


def _extract_voe(url, html):
    """Extract video URL from VOE page. First tries regex, then falls back to Playwright."""
    # Try regex first (fast path - works when VOE doesn't block)
    # Note: patterns must avoid matching JWPlayer tracking/analytics URLs that
    # contain .m3u8 or .mp4 in their query parameters (e.g. jwplayer6/*.gif?...mu=...m3u8...)
    for pattern in [
        r"'(https?://[^']+\.m3u8(?:\?[^']*)?)'",
        r"'(https?://[^']+\.mp4(?:\?[^']*)?)'",
        r'var\s+source\s*=\s*["\']?(https?://[^"\']+)',
    ]:
        for m in re.finditer(pattern, html):
            candidate = m.group(1)
            # Skip tracking pixels, analytics, and JWPlayer beacon URLs
            if any(skip in candidate for skip in ["test-videos", "Big_Buck_Bunny", "/jwplayer", ".gif?"]):
                continue
            return candidate
    # Try base64 encoded
    m = re.search(r"atob\('([^']+)'\)", html)
    if m:
        try:
            decoded = base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
            if decoded.startswith("http") and "test-videos" not in decoded:
                return decoded
        except Exception:
            pass

    # Regex failed (VOE bot protection) → use Playwright headless browser
    log.info(f"VOE: regex failed, trying Playwright for {url}")
    return _extract_stream_playwright(url, "VOE")

# === Async Playwright Browser Pool ===
# Uses playwright.async_api in a dedicated asyncio event loop thread.
# This avoids the greenlet thread-safety issue entirely - all Playwright
# operations run in one thread, but multiple pages can load concurrently.

import asyncio
import concurrent.futures

_pw_loop = None          # dedicated asyncio event loop for Playwright
_pw_loop_thread = None   # thread running the event loop
_pw_browser = None       # persistent async browser instance
_pw_playwright = None    # async playwright context
_pw_last_used = 0.0
_PW_IDLE_TIMEOUT = 300   # close browser after 5min idle


def _ensure_pw_loop():
    """Ensure the Playwright asyncio event loop thread is running."""
    global _pw_loop, _pw_loop_thread
    if _pw_loop and _pw_loop.is_running():
        return _pw_loop
    _pw_loop = asyncio.new_event_loop()
    _pw_loop_thread = threading.Thread(target=_pw_loop.run_forever, daemon=True)
    _pw_loop_thread.start()
    # Start idle checker as async task in the loop
    asyncio.run_coroutine_threadsafe(_pw_idle_checker(), _pw_loop)
    return _pw_loop


async def _get_pw_browser():
    """Get or create a persistent async Playwright browser instance."""
    global _pw_playwright, _pw_browser, _pw_last_used
    _pw_last_used = time.time()
    if _pw_browser and _pw_browser.is_connected():
        return _pw_browser
    # Clean up old instance
    await _cleanup_pw()
    try:
        from playwright.async_api import async_playwright
        _pw_playwright = await async_playwright().start()
        launch_args = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        proxy_settings = {"server": WARP_PROXY} if WARP_PROXY else None
        _pw_browser = await _pw_playwright.chromium.launch(
            headless=True, args=launch_args, proxy=proxy_settings
        )
        log.info("Playwright async browser pool: started")
        return _pw_browser
    except ImportError:
        log.error("Playwright not installed - can't resolve streams")
        return None
    except Exception as e:
        log.error(f"Playwright browser launch failed: {e}")
        await _cleanup_pw()
        return None


async def _cleanup_pw():
    """Close the persistent browser and playwright instance."""
    global _pw_playwright, _pw_browser
    try:
        if _pw_browser:
            await _pw_browser.close()
    except Exception:
        pass
    try:
        if _pw_playwright:
            await _pw_playwright.stop()
    except Exception:
        pass
    _pw_browser = None
    _pw_playwright = None


async def _pw_idle_checker():
    """Async task that closes the browser after idle timeout."""
    while True:
        await asyncio.sleep(60)
        if _pw_browser and time.time() - _pw_last_used > _PW_IDLE_TIMEOUT:
            log.info("Playwright async browser pool: closing (idle timeout)")
            await _cleanup_pw()


# Start the event loop thread
_ensure_pw_loop()


async def _extract_stream_async(url, hoster_name):
    """Extract stream URL using async Playwright. Runs in the dedicated event loop.
    Multiple calls can run concurrently (true async parallelism)."""
    browser = await _get_pw_browser()
    if not browser:
        return None

    page = None
    try:
        page = await browser.new_page()
        stream_url = None
        url_found = asyncio.Event()

        async def handle_request(request):
            nonlocal stream_url
            req_url = request.url
            if stream_url:
                return
            if any(ext in req_url for ext in [".m3u8", ".mp4"]):
                if not any(skip in req_url for skip in [
                    "test-videos", "/jwplayer", ".gif?", "beacon",
                    "Big_Buck_Bunny", "analytics", "tracking"
                ]):
                    stream_url = req_url
                    url_found.set()

        page.on("request", handle_request)
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)

        # Wait for network intercept (fast path) or timeout after 8s
        try:
            await asyncio.wait_for(url_found.wait(), timeout=8)
        except asyncio.TimeoutError:
            pass

        # If network intercept didn't catch it, try JS extraction
        if not stream_url:
            try:
                stream_url = await page.evaluate("""() => {
                    const video = document.querySelector('video source, video');
                    if (video && video.src && video.src.startsWith('http')
                        && !video.src.includes('test-videos')) return video.src;
                    if (typeof jwplayer !== 'undefined') {
                        try {
                            const pl = jwplayer();
                            if (pl && pl.getPlaylistItem) {
                                const item = pl.getPlaylistItem();
                                if (item && item.file) return item.file;
                            }
                        } catch(e) {}
                    }
                    const sources = document.querySelectorAll('source[src]');
                    for (const s of sources) {
                        if (s.src && s.src.startsWith('http')) return s.src;
                    }
                    return null;
                }""")
            except Exception:
                pass

        if stream_url:
            log.info(f"{hoster_name}: Playwright resolved: {stream_url[:80]}...")
        else:
            log.warning(f"{hoster_name}: Playwright could not find stream URL for {url}")
        return stream_url

    except Exception as e:
        log.error(f"{hoster_name}: Playwright extraction failed: {e}")
        if browser and not browser.is_connected():
            await _cleanup_pw()
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


def _extract_stream_playwright(url, hoster_name="unknown"):
    """Thread-safe wrapper: submits async extraction to the Playwright event loop.
    Can be called from any Flask thread - runs truly parallel in the async loop."""
    loop = _ensure_pw_loop()
    future = asyncio.run_coroutine_threadsafe(
        _extract_stream_async(url, hoster_name), loop
    )
    try:
        return future.result(timeout=20)
    except concurrent.futures.TimeoutError:
        log.error(f"{hoster_name}: Playwright extraction timed out (20s) for {url}")
        return None
    except Exception as e:
        log.error(f"{hoster_name}: Playwright extraction error: {e}")
        return None


def extract_video_url(hoster_url, html):
    """Extract video URL from hoster page HTML. Returns URL string or None.
    Only VOE and Vidmoly are supported - other hosters are unreliable."""
    try:
        # voe.sx (and its rotating domains) - always uses Playwright (WASM obfuscation)
        if "voe" in hoster_url:
            return _extract_voe(hoster_url, html)
        # Check if this is a VOE JS-redirect page (VOE uses rotating domains)
        js_redirect = re.search(r"window\.location\.href\s*=\s*'(https?://[^']+)'", html)
        if js_redirect and "/e/" in js_redirect.group(1):
            redirect_url = js_redirect.group(1)
            log.info(f"VOE JS-redirect detected: {redirect_url}")
            return _extract_stream_playwright(redirect_url, "VOE")

        # vidmoly
        if "vidmoly" in hoster_url:
            m = re.search(r'file:\s*["\']?(https?://[^"\']+\.m3u8[^"\']*)["\']?', html)
            if m:
                return m.group(1)
            m = re.search(r'sources:\s*\[\{\s*file:\s*["\']?(https?://[^"\']+)["\']?', html)
            if m:
                return m.group(1)
            m = re.search(r'<source\s+src=["\']?(https?://[^"\']+)["\']?', html)
            if m:
                return m.group(1)
            log.info(f"Vidmoly: regex failed, trying Playwright for {hoster_url}")
            return _extract_stream_playwright(hoster_url, "Vidmoly")

        # Unsupported hoster - skip
        log.debug(f"Skipping unsupported hoster: {hoster_url}")
        return None

    except Exception as e:
        log.error(f"Error extracting video URL from {hoster_url}: {e}")
        return None

# --- Scraper ---

def sync_catalog():
    log.info("Starting catalog sync...")
    conn = get_conn()

    conn.execute("INSERT INTO sync_log (started_at) VALUES (?)", (datetime.now(tz=timezone.utc).isoformat(),))
    sync_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    try:
        resp = _rate_limited_get(f"{BASE_URL}/animes-alphabet")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        links = soup.select('a[href^="/anime/stream/"]')
        total = len(links)
        log.info(f"Found {total} anime entries")

        updated = 0
        for a in links:
            href = a.get("href", "")
            title = unescape(a.get_text(strip=True))
            slug = href.replace("/anime/stream/", "").strip("/")
            if not slug or not title:
                continue

            letter = title[0].upper() if title[0].isalpha() else "#"

            conn.execute("""
                INSERT INTO anime (slug, title, letter) VALUES (?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET title=?, letter=?, updated_at=datetime('now')
            """, (slug, title, letter, title, letter))
            updated += 1

        conn.execute("""
            UPDATE sync_log SET finished_at=?, status='done',
            entries_total=?, entries_updated=? WHERE id=?
        """, (datetime.now(tz=timezone.utc).isoformat(), total, updated, sync_id))
        conn.commit()
        log.info(f"Catalog sync done: {updated}/{total}")
        return updated

    except Exception as e:
        conn.execute("UPDATE sync_log SET finished_at=?, status='error', error=? WHERE id=?",
                     (datetime.now(tz=timezone.utc).isoformat(), str(e), sync_id))
        conn.commit()
        log.error(f"Sync failed: {e}")
        raise
    finally:
        conn.close()

def sync_anime_details(slug):
    log.info(f"Syncing details for {slug}")

    resp = _rate_limited_get(f"{BASE_URL}/anime/stream/{slug}")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Movies tab
    has_movies = 1 if soup.select_one('a[href*="/filme"]') else 0

    # Description
    desc_el = soup.select_one("p.seri_des")
    description = desc_el.get("data-full-description") or desc_el.get_text(strip=True) if desc_el else None

    # Cover
    cover_el = soup.select_one("div.seriesCoverBox img")
    cover_url = cover_el.get("data-src") or cover_el.get("src") if cover_el else None

    # Seasons
    season_links = soup.select('a[href*="/staffel-"]')
    season_nums = set()
    for sl in season_links:
        m = re.search(r"staffel-(\d+)", sl.get("href", ""))
        if m:
            season_nums.add(int(m.group(1)))

    conn = get_conn()
    try:
        conn.execute("""
            UPDATE anime SET has_movies=?, description=?, cover_url=?,
            season_count=?, last_scraped=?, updated_at=datetime('now')
            WHERE slug=?
        """, (has_movies, description, cover_url, len(season_nums),
              datetime.now(tz=timezone.utc).isoformat(), slug))

        for sn in season_nums:
            conn.execute("""
                INSERT INTO season (anime_slug, season_number) VALUES (?, ?)
                ON CONFLICT(anime_slug, season_number) DO NOTHING
            """, (slug, sn))

        conn.commit()
    finally:
        conn.close()

def _needs_detail_scrape(last_scraped):
    """Check if details are stale or missing."""
    if not last_scraped:
        return True
    try:
        scraped_dt = _parse_dt(last_scraped)
        return datetime.now(tz=timezone.utc) - scraped_dt > timedelta(days=DETAIL_CACHE_DAYS)
    except Exception:
        return True

def sync_details_batch():
    """Scrape details for anime entries that haven't been scraped yet. Runs in background."""
    global _detail_sync_running
    if _detail_sync_running:
        return 0

    _detail_sync_running = True
    scraped = 0
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT slug FROM anime WHERE last_scraped IS NULL ORDER BY title LIMIT ?",
            (DETAIL_BATCH_SIZE,)
        ).fetchall()
        conn.close()

        remaining = len(rows)
        if remaining == 0:
            log.info("Detail sync: all entries already scraped")
            return 0

        log.info(f"Detail sync: scraping {remaining} entries...")

        for row in rows:
            try:
                sync_anime_details(row["slug"])
                scraped += 1
            except Exception as e:
                log.warning(f"Detail scrape failed for {row['slug']}: {e}")
                # Mark as scraped anyway to avoid retry loop
                c = get_conn()
                c.execute("UPDATE anime SET last_scraped=? WHERE slug=?",
                          (datetime.now(tz=timezone.utc).isoformat(), row["slug"]))
                c.commit()
                c.close()

        log.info(f"Detail sync done: {scraped}/{remaining}")
    except Exception as e:
        log.error(f"Detail sync batch failed: {e}")
    finally:
        _detail_sync_running = False
    return scraped

# --- Full & Incremental Sync ---

def full_sync():
    """Full scrape of all episodes for all known anime. Returns summary dict."""
    log.info("Starting full sync...")
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT DISTINCT a.slug, a.has_movies, s.season_number
            FROM anime a
            LEFT JOIN season s ON a.slug = s.anime_slug
            WHERE a.last_scraped IS NOT NULL
            ORDER BY a.slug, s.season_number
        """).fetchall()
    finally:
        conn.close()

    total = len(rows)
    scraped = 0
    processed_slugs = set()

    for row in rows:
        try:
            slug = row["slug"]
            season_num = row["season_number"]
            has_movies = row["has_movies"]

            if has_movies and slug not in processed_slugs:
                try:
                    scrape_film_episodes(slug)
                except Exception as e:
                    log.warning(f"Full sync: failed films for {slug}: {e}")

            processed_slugs.add(slug)

            if season_num is not None:
                try:
                    scrape_season_episodes(slug, season_num)
                except Exception as e:
                    log.warning(f"Full sync: failed {slug} S{season_num}: {e}")

            scraped += 1
            if scraped % 10 == 0:
                log.info(f"Full sync progress: {scraped}/{total}")

        except Exception as e:
            log.error(f"Full sync error for row: {e}")
            continue

    log.info(f"Full sync done: {scraped}/{total} entries")
    return {"mode": "full", "scraped": scraped, "total": total}


def incremental_sync():
    """Incremental sync (used by Dashboard button + Nightly):
    1. Fetch /animes-alphabet → find new anime not yet in DB → fully scrape them.
    2. ALL existing anime: fetch detail page, compare season_count.
       - New seasons → scrape those.
       - Same season count → re-scrape latest season to check for new episodes.
       - Also checks for new films.
    No time filter - always checks everything for changes.
    Returns dict: {new_anime, updated_anime, errors}.
    """
    results = {"mode": "incremental", "new_anime": 0, "updated_anime": 0, "errors": 0}
    log.info("Starting incremental sync...")

    # Step 1: Fetch alphabet page
    try:
        resp = _rate_limited_get(f"{BASE_URL}/animes-alphabet", delay=INCREMENTAL_CHECK_DELAY)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.error(f"Incremental sync: failed to fetch alphabet: {e}")
        results["errors"] += 1
        return results

    links = soup.select('a[href^="/anime/stream/"]')
    live_slugs = {}
    for a in links:
        href = a.get("href", "")
        title = unescape(a.get_text(strip=True))
        slug = href.replace("/anime/stream/", "").strip("/")
        if slug and title:
            live_slugs[slug] = title

    log.info(f"Incremental: {len(live_slugs)} anime live on aniworld.to")

    # Step 2: Compare with DB
    conn = get_conn()
    try:
        existing_rows = conn.execute("SELECT slug, season_count FROM anime").fetchall()
        existing = {r["slug"]: (r["season_count"] or 0) for r in existing_rows}
    finally:
        conn.close()

    new_slugs = {s: t for s, t in live_slugs.items() if s not in existing}
    log.info(f"Incremental: {len(new_slugs)} new anime, {len(existing)} existing")

    # Step 3: Scrape new anime fully
    new_total = len(new_slugs)
    new_checked = 0
    for slug, title in new_slugs.items():
        new_checked += 1
        _incremental_sync_status["progress"] = {
            "phase": "new_anime",
            "checked": new_checked,
            "total": new_total,
            "current_slug": slug,
            "new_anime": results["new_anime"],
        }
        try:
            letter = title[0].upper() if title[0].isalpha() else "#"
            conn = get_conn()
            conn.execute(
                "INSERT INTO anime (slug, title, letter) VALUES (?, ?, ?) ON CONFLICT(slug) DO NOTHING",
                (slug, title, letter)
            )
            conn.commit()
            conn.close()

            sync_anime_details(slug)

            conn = get_conn()
            seasons = conn.execute(
                "SELECT season_number FROM season WHERE anime_slug=?", (slug,)
            ).fetchall()
            has_movies_row = conn.execute(
                "SELECT has_movies FROM anime WHERE slug=?", (slug,)
            ).fetchone()
            conn.close()

            for s in seasons:
                scrape_season_episodes(slug, s["season_number"])
            if has_movies_row and has_movies_row["has_movies"]:
                scrape_film_episodes(slug)

            results["new_anime"] += 1
            log.info(f"Incremental: new anime scraped: {slug}")
            _log_change(slug, title, "new_anime", "Neu auf AniWorld")
        except Exception as e:
            log.error(f"Incremental: failed to scrape new anime {slug}: {e}")
            results["errors"] += 1

    # Step 4: Check existing anime for new episodes/seasons
    # Only check RELEASING / unknown status anime (skip FINISHED)
    status_map = {}
    try:
        status_resp = requests.get(f"{METADATA_BASE}/api/status/bulk", timeout=10)
        if status_resp.ok:
            status_map = status_resp.json()
            log.info(f"Incremental: got status for {len(status_map)} anime from metadata server")
    except Exception as e:
        log.warning(f"Incremental: could not fetch status from metadata server: {e} - checking all anime")

    conn = get_conn()
    try:
        all_anime = conn.execute("""
            SELECT a.slug, a.season_count, a.has_movies
            FROM anime a
            WHERE a.last_scraped IS NOT NULL
            ORDER BY a.slug ASC
        """).fetchall()
    finally:
        conn.close()

    # Filter: only check anime that are NOT finished
    to_check = []
    skipped_finished = 0
    for row in all_anime:
        status = status_map.get(row["slug"])
        if status == "FINISHED":
            skipped_finished += 1
        else:
            # RELEASING, NOT_YET_RELEASED, None/unknown → check
            to_check.append(row)

    total_to_check = len(to_check)
    log.info(f"Incremental: checking {total_to_check} anime for updates (skipped {skipped_finished} finished)")
    _incremental_sync_status["progress"] = {
        "phase": "checking",
        "checked": 0,
        "total": total_to_check,
        "skipped_finished": skipped_finished,
        "new_anime": results["new_anime"],
        "updated": 0,
        "errors": 0,
        "current_slug": "",
    }
    checked = 0

    for row in to_check:
        checked += 1
        _incremental_sync_status["progress"] = {
            "checked": checked,
            "total": total_to_check,
            "skipped_finished": skipped_finished,
            "new_anime": results["new_anime"],
            "updated": results["updated_anime"],
            "errors": results["errors"],
            "current_slug": row["slug"],
        }
        if checked % 100 == 0:
            log.info(f"Incremental: progress {checked}/{total_to_check} ({results['updated_anime']} updated so far)")
        slug = row["slug"]
        old_season_count = row["season_count"] or 0

        try:
            # Fetch detail page with fast delay
            resp = _rate_limited_get(
                f"{BASE_URL}/anime/stream/{slug}",
                delay=INCREMENTAL_CHECK_DELAY
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            season_links = soup.select('a[href*="/staffel-"]')
            live_season_nums = set()
            for sl in season_links:
                m = re.search(r"staffel-(\d+)", sl.get("href", ""))
                if m:
                    live_season_nums.add(int(m.group(1)))

            live_season_count = len(live_season_nums)

            # Update season_count and season entries in DB
            conn = get_conn()
            try:
                conn.execute("""
                    UPDATE anime SET season_count=?, last_scraped=?, updated_at=datetime('now')
                    WHERE slug=?
                """, (live_season_count, datetime.now(tz=timezone.utc).isoformat(), slug))
                for sn in live_season_nums:
                    conn.execute("""
                        INSERT INTO season (anime_slug, season_number) VALUES (?, ?)
                        ON CONFLICT(anime_slug, season_number) DO NOTHING
                    """, (slug, sn))
                conn.commit()

                db_seasons = conn.execute(
                    "SELECT season_number, episode_count FROM season WHERE anime_slug=? ORDER BY season_number DESC",
                    (slug,)
                ).fetchall()
            finally:
                conn.close()

            anime_updated = False

            if live_season_count > old_season_count and live_season_nums:
                # New seasons found → scrape only the new ones
                conn = get_conn()
                existing_with_eps = {
                    r["season_number"] for r in conn.execute(
                        "SELECT season_number FROM season WHERE anime_slug=? AND episode_count > 0",
                        (slug,)
                    ).fetchall()
                }
                conn.close()

                new_seasons = live_season_nums - existing_with_eps
                for sn in sorted(new_seasons):
                    count = scrape_season_episodes(slug, sn)
                    if count > 0:
                        anime_updated = True
                        _log_change(slug, None, "new_season", f"Staffel {sn} ({count} Episoden)")
            elif db_seasons and live_season_nums:
                # Same season count: quick-check last season episode count
                # Only fetch the season page to COUNT episodes, don't scrape yet
                last_season = db_seasons[0]  # DESC order → [0] is highest season
                old_ep_count = last_season["episode_count"] or 0
                last_sn = last_season["season_number"]

                try:
                    season_resp = _rate_limited_get(
                        f"{BASE_URL}/anime/stream/{slug}/staffel-{last_sn}",
                        delay=INCREMENTAL_CHECK_DELAY
                    )
                    season_resp.raise_for_status()
                    season_soup = BeautifulSoup(season_resp.text, "html.parser")
                    live_ep_count = len(season_soup.select("table.seasonEpisodesList tr[data-episode-id]"))

                    if live_ep_count > old_ep_count:
                        # New episodes found → now actually scrape them
                        new_count = live_ep_count - old_ep_count
                        log.info(f"Incremental: {slug} S{last_sn} has new episodes ({old_ep_count} → {live_ep_count})")
                        scrape_season_episodes(slug, last_sn)
                        anime_updated = True
                        _log_change(slug, None, "new_episodes", f"S{last_sn:02d}: {new_count} neue Episode{'n' if new_count != 1 else ''}")
                except Exception as e:
                    log.warning(f"Incremental: quick-check failed for {slug} S{last_sn}: {e}")

            # Check for new films (quick-count from film page)
            has_movies_link = soup.select_one('a[href*="/filme"]')
            if has_movies_link:
                conn = get_conn()
                try:
                    conn.execute("UPDATE anime SET has_movies=1 WHERE slug=?", (slug,))
                    conn.commit()
                    old_film_count = conn.execute(
                        "SELECT COUNT(*) as cnt FROM episode WHERE anime_slug=? AND season_number=0",
                        (slug,)
                    ).fetchone()["cnt"]
                finally:
                    conn.close()

                # Quick-count films without full scrape
                try:
                    film_resp = _rate_limited_get(
                        f"{BASE_URL}/anime/stream/{slug}/filme",
                        delay=INCREMENTAL_CHECK_DELAY
                    )
                    film_resp.raise_for_status()
                    film_soup = BeautifulSoup(film_resp.text, "html.parser")
                    live_film_count = len(film_soup.select("tr[data-episode-id]"))

                    if live_film_count > old_film_count:
                        new_films = live_film_count - old_film_count
                        log.info(f"Incremental: {slug} has new films ({old_film_count} → {live_film_count})")
                        scrape_film_episodes(slug)
                        anime_updated = True
                        _log_change(slug, None, "new_films", f"{new_films} neue{'r' if new_films == 1 else ''} Film{'e' if new_films != 1 else ''}")
                except Exception as e:
                    log.warning(f"Incremental: film quick-check failed for {slug}: {e}")

            if anime_updated:
                results["updated_anime"] += 1

        except Exception as e:
            log.error(f"Incremental: failed to check existing anime {slug}: {e}")
            results["errors"] += 1

    log.info(f"Incremental sync done: {results}")

    # Trigger Emby Library Scan if changes were found
    if results["new_anime"] > 0 or results["updated_anime"] > 0:
        _trigger_emby_library_scan()

    return results


# --- Background sync ---

def bg_sync_loop():
    """Background loop: only syncs the catalog (anime list from alphabet page).
    Detail scraping only runs on first install or manual 'Batch Scrape' button."""
    time.sleep(5)
    while True:
        try:
            sync_catalog()
        except Exception:
            pass

        time.sleep(SYNC_INTERVAL)

def nightly_episode_scrape():
    """Background thread that scrapes all episodes daily at 02:00 UTC."""
    if schedule is None:
        log.warning("schedule module not available - nightly scrape disabled")
        return
    
    def scrape_job():
        log.info("Nightly incremental sync triggered (02:00 UTC)")
        try:
            incremental_sync()
        except Exception as e:
            log.error(f"Nightly incremental sync failed: {e}")

        conn = get_conn()
        try:
            log.info(f"Nightly incremental sync completed")

            # Phase 2: Pre-resolve stream URLs
            # Priority: episode 1 of every season first, then remaining episodes
            log.info("Nightly stream pre-resolve: starting (episode 1 of each season first)")
            try:
                all_eps = conn.execute("""
                    SELECT anime_slug, season_number, episode_number
                    FROM episode
                    ORDER BY
                        CASE WHEN episode_number = 1 THEN 0 ELSE 1 END,
                        anime_slug, season_number, episode_number
                """).fetchall()

                total_eps = len(all_eps)
                resolved = 0
                skipped = 0
                for ep in all_eps:
                    s, sn, en = ep["anime_slug"], ep["season_number"], ep["episode_number"]
                    try:
                        hosters = _get_hoster_cache(s, sn, en)
                        if hosters is None:
                            hosters = _scrape_hoster_list(s, sn, en)
                            resolved += 1
                        else:
                            skipped += 1
                        # For episode 1: also pre-resolve CDN URLs so first click is instant
                        # Resolve ALL hosters that have redirect_url but no stream_url yet
                        if hosters and en == 1:
                            for h in hosters:
                                if not h.get("streamUrl") and h.get("redirectUrl"):
                                    # Skip recently failed (within 6h)
                                    if h.get("failedAt"):
                                        try:
                                            if datetime.now(tz=timezone.utc) - _parse_dt(h["failedAt"]) < timedelta(minutes=HOSTER_FAIL_TTL_MINUTES):
                                                continue
                                        except Exception:
                                            pass
                                    r = _resolve_redirect_to_stream(h)
                                    if r:
                                        _update_stream_url_cache(s, sn, en, h["name"], h["langKey"], r["streamUrl"])
                                    else:
                                        _mark_hoster_failed(s, sn, en, h["name"], h["langKey"])
                        if resolved % 50 == 0:
                            log.info(f"Stream pre-resolve: {resolved} done, {skipped} skipped, {total_eps - resolved - skipped} remaining")
                    except Exception as e:
                        log.warning(f"Stream pre-resolve failed {s} S{sn}E{en}: {e}")

                log.info(f"Nightly stream pre-resolve done: {resolved} resolved, {skipped} already cached")
            except Exception as e:
                log.error(f"Nightly stream pre-resolve failed: {e}")

        except Exception as e:
            log.error(f"Nightly episode scrape failed: {e}")
        finally:
            conn.close()
    
    def stream_refresh_job():
        """Refresh CDN stream URLs for recently-accessed episodes (last 24h) that are >1.5h old.
        This keeps active watch sessions instant. Runs every 30 min."""
        conn = get_conn()
        try:
            stale = conn.execute("""
                SELECT DISTINCT slug, season, episode FROM stream_cache
                WHERE last_accessed > datetime('now', '-24 hours')
                AND stream_url IS NOT NULL
                AND stream_cached_at < datetime('now', '-90 minutes')
            """).fetchall()
        finally:
            conn.close()

        if not stale:
            return

        log.info(f"CDN refresh: {len(stale)} recently-accessed entries to refresh")
        HOSTER_PRIORITY = ["VOE", "Vidmoly"]
        LANG_PRIORITY = [1, 3, 2]
        for row in stale:
            try:
                # Re-resolve CDN URL for each language (preferred hoster only)
                hosters = _get_hoster_cache(row["slug"], row["season"], row["episode"])
                if hosters:
                    for lk in LANG_PRIORITY:
                        for hoster_name in HOSTER_PRIORITY:
                            h = next((x for x in hosters if x["langKey"] == lk and x["name"] == hoster_name), None)
                            if h is None:
                                continue
                            resolved = _resolve_redirect_to_stream(h)
                            if resolved:
                                _update_stream_url_cache(row["slug"], row["season"], row["episode"], h["name"], lk, resolved["streamUrl"])
                                break  # got one for this lang, move on
            except Exception as e:
                log.warning(f"CDN refresh failed for {row['slug']} S{row['season']}E{row['episode']}: {e}")

    schedule.every().day.at("02:00").do(scrape_job)
    # Stream refresh disabled - CDN URLs are resolved on-demand when playing.
    # On small servers (2GB RAM), background Playwright sessions cause OOM.
    # schedule.every(30).minutes.do(stream_refresh_job)

    log.info("Nightly episode scrape scheduler started (02:00 UTC)")
    log.info("Stream refresh disabled (on-demand resolve only)")

    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# --- Routes ---

@app.route("/api/status")
def status():
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM anime").fetchone()[0]
    scraped = db.execute("SELECT COUNT(*) FROM anime WHERE last_scraped IS NOT NULL").fetchone()[0]
    unscraped = count - scraped
    row = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    last_sync = None
    if row:
        last_sync = {
            "started": row["started_at"],
            "finished": row["finished_at"],
            "status": row["status"],
            "total": row["entries_total"],
            "updated": row["entries_updated"],
        }
    return jsonify({
        "animeCount": count,
        "detailsScraped": scraped,
        "detailsPending": unscraped,
        "detailSyncRunning": _detail_sync_running,
        "lastSync": last_sync
    })

@app.route("/api/letters")
def letters():
    db = get_db()
    rows = db.execute("SELECT letter, COUNT(*) as cnt FROM anime GROUP BY letter ORDER BY letter").fetchall()
    result = []
    for r in rows:
        first = db.execute("SELECT slug FROM anime WHERE letter=? ORDER BY title LIMIT 1", (r["letter"],)).fetchone()
        result.append({"letter": r["letter"], "count": r["cnt"], "first_slug": first["slug"] if first else None})
    return jsonify(result)

@app.route("/api/anime")
def anime_list():
    letter = request.args.get("letter", "").upper()
    movies_only = request.args.get("movies", "").lower() == "true"
    db = get_db()

    sql = "SELECT slug, title, letter, has_movies, season_count, cover_url FROM anime"
    params = []
    wheres = []

    if letter:
        wheres.append("letter=?")
        params.append(letter)
    if movies_only:
        wheres.append("has_movies=1")

    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY title"

    rows = db.execute(sql, params).fetchall()
    return jsonify([{
        "slug": r["slug"], "title": r["title"], "letter": r["letter"],
        "hasMovies": bool(r["has_movies"]), "seasonCount": r["season_count"],
        "coverUrl": r["cover_url"]
    } for r in rows])

@app.route("/api/anime/<slug>")
def anime_detail(slug):
    """?cached=1 skips freshness check (for sync)."""
    db = get_db()
    row = db.execute("SELECT * FROM anime WHERE slug=?", (slug,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    # On-demand detail scrape if stale or never scraped
    cached_only = request.args.get("cached") == "1"
    if not cached_only and _needs_detail_scrape(row["last_scraped"]):
        if row["last_scraped"] is None:
            # Never scraped: block once to get initial data
            try:
                sync_anime_details(slug)
                row = db.execute("SELECT * FROM anime WHERE slug=?", (slug,)).fetchone()
            except Exception as e:
                log.warning(f"On-demand scrape failed for {slug}: {e}")
        else:
            # Data exists but stale: refresh in background, return cached data immediately
            def background_scrape(s):
                try:
                    sync_anime_details(s)
                except Exception as e:
                    log.warning(f"Background scrape failed for {s}: {e}")
            threading.Thread(target=background_scrape, args=(slug,), daemon=True).start()

    seasons = db.execute(
        "SELECT season_number, episode_count FROM season WHERE anime_slug=? ORDER BY season_number",
        (slug,)
    ).fetchall()

    return jsonify({
        "slug": row["slug"], "title": row["title"], "letter": row["letter"],
        "hasMovies": bool(row["has_movies"]), "description": row["description"],
        "coverUrl": row["cover_url"], "seasonCount": row["season_count"],
        "lastScraped": row["last_scraped"],
        "seasons": [{"number": s["season_number"], "episodes": s["episode_count"]} for s in seasons]
    })

@app.route("/api/anime/recent")
def get_recent_anime():
    """Returns the most recently added anime (by insertion order)."""
    limit = request.args.get("limit", 50, type=int)
    db = get_db()
    rows = db.execute(
        "SELECT slug, title FROM anime ORDER BY rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return jsonify([{"slug": r["slug"], "title": r["title"]} for r in rows])


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    db = get_db()
    # SQLite LIKE search with wildcards
    pattern = f"%{query}%"
    rows = db.execute(
        "SELECT slug, title, letter, has_movies, season_count, cover_url FROM anime WHERE title LIKE ? ORDER BY title LIMIT 50",
        (pattern,)
    ).fetchall()
    return jsonify([{
        "slug": r["slug"], "title": r["title"], "letter": r["letter"],
        "hasMovies": bool(r["has_movies"]), "seasonCount": r["season_count"],
        "coverUrl": r["cover_url"]
    } for r in rows])

@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    count = sync_catalog()
    return jsonify({"synced": count})

@app.route("/api/sync/details", methods=["POST"])
def trigger_detail_sync():
    """Trigger background detail scraping."""
    t = threading.Thread(target=sync_details_batch, daemon=True)
    t.start()
    return jsonify({"status": "started", "batchSize": DETAIL_BATCH_SIZE})

@app.route("/api/anime/<slug>/season/<int:season_num>/episodes")
def get_season_episodes(slug, season_num):
    """Get episode list for a specific season. Scrapes on-demand if stale or missing.
    ?cached=1 skips freshness check (for sync - just return DB data)."""
    db = get_db()
    
    # Check if episodes exist and are fresh (< 24h old)
    rows = db.execute("""
        SELECT episode_number, title, title_en, url, last_scraped
        FROM episode
        WHERE anime_slug=? AND season_number=?
        ORDER BY episode_number
    """, (slug, season_num)).fetchall()
    
    cached_only = request.args.get("cached") == "1"
    needs_scrape = False
    if not cached_only:
        if not rows:
            needs_scrape = True
        else:
            # Check staleness
            last_scraped = rows[0]["last_scraped"] if rows else None
            if last_scraped:
                try:
                    scraped_dt = _parse_dt(last_scraped)
                    if datetime.now(tz=timezone.utc) - scraped_dt > timedelta(hours=24):
                        needs_scrape = True
                except Exception:
                    needs_scrape = True
            else:
                needs_scrape = True
    
    if needs_scrape:
        if not rows:
            # No data at all: block to get initial data
            try:
                scrape_season_episodes(slug, season_num)
                rows = db.execute("""
                    SELECT episode_number, title, title_en, url, last_scraped
                    FROM episode
                    WHERE anime_slug=? AND season_number=?
                    ORDER BY episode_number
                """, (slug, season_num)).fetchall()
            except Exception as e:
                log.error(f"On-demand scrape failed for {slug} S{season_num}: {e}")
        else:
            # Stale data: refresh in background, return cached immediately
            def bg_scrape(s, sn):
                try:
                    scrape_season_episodes(s, sn)
                except Exception as e:
                    log.warning(f"Background episode scrape failed {s} S{sn}: {e}")
            threading.Thread(target=bg_scrape, args=(slug, season_num), daemon=True).start()
    
    episodes = [{
        "episodeNumber": r["episode_number"],
        "title": r["title"],
        "titleEn": r["title_en"],
        "url": r["url"],
        "lastScraped": r["last_scraped"]
    } for r in rows]

    # Pre-resolve only when explicitly requested (not during sync - kills RAM with Playwright)
    if request.args.get("prefetch") == "1":
        ep_numbers = [r["episode_number"] for r in rows]
        def prefetch_streams(s, sn, eps):
            for ep_num in eps:
                try:
                    if _get_hoster_cache(s, sn, ep_num) is None:
                        _scrape_hoster_list(s, sn, ep_num)
                except Exception as e:
                    log.warning(f"Prefetch stream failed {s} S{sn}E{ep_num}: {e}")
        threading.Thread(target=prefetch_streams, args=(slug, season_num, ep_numbers), daemon=True).start()

    return jsonify(episodes)

@app.route("/api/anime/<slug>/films/episodes")
def get_film_episodes(slug):
    """Get film/movie list (season_number=0). Scrapes on-demand if stale or missing.
    ?cached=1 skips freshness check (for sync)."""
    db = get_db()
    
    # Check if films exist and are fresh (< 24h old)
    rows = db.execute("""
        SELECT episode_number, title, title_en, url, last_scraped
        FROM episode
        WHERE anime_slug=? AND season_number=0
        ORDER BY episode_number
    """, (slug,)).fetchall()
    
    cached_only = request.args.get("cached") == "1"
    needs_scrape = False
    if not cached_only:
        if not rows:
            needs_scrape = True
        else:
            last_scraped = rows[0]["last_scraped"] if rows else None
            if last_scraped:
                try:
                    scraped_dt = _parse_dt(last_scraped)
                    if datetime.now(tz=timezone.utc) - scraped_dt > timedelta(hours=24):
                        needs_scrape = True
                except Exception:
                    needs_scrape = True
            else:
                needs_scrape = True
    
    if needs_scrape:
        if not rows:
            # No data at all: block to get initial data
            try:
                scrape_film_episodes(slug)
                rows = db.execute("""
                    SELECT episode_number, title, title_en, url, last_scraped
                    FROM episode
                    WHERE anime_slug=? AND season_number=0
                    ORDER BY episode_number
                """, (slug,)).fetchall()
            except Exception as e:
                log.error(f"On-demand scrape failed for films {slug}: {e}")
        else:
            # Stale data: refresh in background, return cached immediately
            def bg_scrape_films(s):
                try:
                    scrape_film_episodes(s)
                except Exception as e:
                    log.warning(f"Background film scrape failed {s}: {e}")
            threading.Thread(target=bg_scrape_films, args=(slug,), daemon=True).start()
    
    episodes = [{
        "episodeNumber": r["episode_number"],
        "title": r["title"],
        "titleEn": r["title_en"],
        "url": r["url"],
        "lastScraped": r["last_scraped"]
    } for r in rows]

    # Pre-resolve only when explicitly requested
    if request.args.get("prefetch") == "1":
        film_ep_numbers = [r["episode_number"] for r in rows]
        def prefetch_film_streams(s, eps):
            for ep_num in eps:
                try:
                    if _get_hoster_cache(s, 0, ep_num) is None:
                        _scrape_hoster_list(s, 0, ep_num)
                except Exception as e:
                    log.warning(f"Prefetch film stream failed {s} E{ep_num}: {e}")
        threading.Thread(target=prefetch_film_streams, args=(slug, film_ep_numbers), daemon=True).start()

    return jsonify(episodes)

@app.route("/api/resolve", methods=["POST"])
def resolve_streams():
    """Resolve stream URLs for a specific episode. No caching - URLs are short-lived."""
    data = request.get_json() or {}
    slug = data.get("slug")
    season = data.get("season")
    episode = data.get("episode")
    
    if not slug or season is None or episode is None:
        return jsonify({"error": "Missing required fields: slug, season, episode"}), 400
    
    try:
        hosters = resolve_stream_urls(slug, season, episode)
        return jsonify(hosters)
    except Exception as e:
        log.error(f"Stream resolution failed for {slug} S{season}E{episode}: {e}")
        return jsonify({"error": str(e)}), 500

_full_sync_status = {"running": False, "result": None, "started_at": None, "finished_at": None}

@app.route("/api/sync/full", methods=["POST"])
def trigger_full_sync():
    """Trigger a full scrape of all episode lists. Runs in background. ~2-3h."""
    if _full_sync_status["running"]:
        return jsonify({"status": "already_running", "mode": "full"}), 409

    def _run():
        _full_sync_status["running"] = True
        _full_sync_status["started_at"] = datetime.now(tz=timezone.utc).isoformat()
        _full_sync_status["result"] = None
        _full_sync_status["finished_at"] = None
        log.info("Full sync started (triggered via API)")
        try:
            result = full_sync()
            _full_sync_status["result"] = result if result else {"mode": "full", "status": "done"}
        except Exception as e:
            log.error(f"Full sync failed: {e}")
            _full_sync_status["result"] = {"mode": "full", "error": str(e)}
        finally:
            _full_sync_status["running"] = False
            _full_sync_status["finished_at"] = datetime.now(tz=timezone.utc).isoformat()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "mode": "full"})

@app.route("/api/sync/full/status")
def get_full_sync_status():
    """Get current full sync status + result."""
    return jsonify(_full_sync_status)


_incremental_sync_status = {"running": False, "result": None, "started_at": None, "finished_at": None, "progress": None}

@app.route("/api/sync/incremental", methods=["POST"])
def trigger_incremental_sync():
    """Trigger incremental sync: new anime + episode count checks. Runs in background."""
    if _incremental_sync_status["running"]:
        return jsonify({"status": "already_running", "mode": "incremental"}), 409

    def _run():
        _incremental_sync_status["running"] = True
        _incremental_sync_status["started_at"] = datetime.now(tz=timezone.utc).isoformat()
        _incremental_sync_status["result"] = None
        _incremental_sync_status["finished_at"] = None
        _incremental_sync_status["progress"] = None
        log.info("Incremental sync started (triggered via API)")
        try:
            result = incremental_sync()
            _incremental_sync_status["result"] = result
        except Exception as e:
            log.error(f"Incremental sync failed: {e}")
            _incremental_sync_status["result"] = {"mode": "incremental", "error": str(e)}
        finally:
            _incremental_sync_status["running"] = False
            _incremental_sync_status["finished_at"] = datetime.now(tz=timezone.utc).isoformat()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "mode": "incremental"})

@app.route("/api/sync/incremental/status")
def get_incremental_sync_status():
    """Get current incremental sync status + result."""
    return jsonify(_incremental_sync_status)


# Backward-compat alias: old plugin versions call /api/scrape/episodes
@app.route("/api/scrape/detail/<slug>", methods=["POST"])
def trigger_single_detail_scrape(slug):
    """Scrape details for a single anime by slug."""
    db = get_db()
    row = db.execute("SELECT slug, title FROM anime WHERE slug=?", (slug,)).fetchone()
    if not row:
        return jsonify({"error": f"Anime '{slug}' nicht gefunden"}), 404
    try:
        sync_anime_details(slug)
        return jsonify({"status": "ok", "slug": slug, "title": row["title"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrape/episodes", methods=["POST"])
def trigger_episode_scrape():
    """Backward-compat alias for /api/sync/full."""
    return trigger_full_sync()


@app.route("/api/hoster-health")
def get_hoster_health():
    """Get hoster health stats (success rate, total, failed, last seen)."""
    conn = get_conn()
    try:
        rows = conn.execute(f"""
            SELECT
                hoster,
                COUNT(*) as total,
                COUNT(CASE WHEN stream_url IS NOT NULL AND stream_url != '' THEN 1 END) as resolved,
                COUNT(CASE WHEN failed_at IS NOT NULL AND failed_at > datetime('now', '-{HOSTER_FAIL_TTL_MINUTES} minutes') THEN 1 END) as recent_failures,
                MAX(stream_cached_at) as last_success,
                MAX(failed_at) as last_failure
            FROM stream_cache
            GROUP BY hoster
            ORDER BY total DESC
        """).fetchall()
    finally:
        conn.close()

    return jsonify([{
        "name": r["hoster"],
        "total": r["total"],
        "resolved": r["resolved"],
        "recentFailures": r["recent_failures"],
        "successRate": round((r["resolved"] / r["total"]) * 100, 1) if r["total"] > 0 else 0,
        "lastSuccess": r["last_success"],
        "lastFailure": r["last_failure"],
    } for r in rows])


@app.route("/api/cache/clear-failed", methods=["POST"])
def clear_failed_cache():
    """Reset failed_at Marks - alle oder für einen bestimmten Slug."""
    slug = request.json.get("slug") if request.is_json else None
    conn = get_conn()
    try:
        if slug:
            r = conn.execute("UPDATE stream_cache SET failed_at=NULL WHERE slug=? AND failed_at IS NOT NULL", (slug,)).rowcount
        else:
            r = conn.execute("UPDATE stream_cache SET failed_at=NULL WHERE failed_at IS NOT NULL").rowcount
        conn.commit()
    finally:
        conn.close()
    log.info(f"Cleared {r} failed marks" + (f" for {slug}" if slug else ""))
    return jsonify({"cleared": r, "slug": slug})


@app.route("/api/changes")
def get_recent_changes():
    """Get recent changes (new anime, new episodes, etc.). Query: ?days=7&limit=100"""
    days = request.args.get("days", default=7, type=int)
    limit = request.args.get("limit", default=100, type=int)
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT slug, title, change_type, detail, created_at
            FROM recent_changes
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT ?
        """, (f"-{days} days", limit)).fetchall()
    finally:
        conn.close()
    return jsonify([{
        "slug": r["slug"],
        "title": r["title"],
        "changeType": r["change_type"],
        "detail": r["detail"],
        "createdAt": r["created_at"],
    } for r in rows])


if __name__ == "__main__":
    init_db()
    
    # Start background sync threads
    t = threading.Thread(target=bg_sync_loop, daemon=True)
    t.start()
    
    t2 = threading.Thread(target=nightly_episode_scrape, daemon=True)
    t2.start()
    
    port = _cfg.getint("api", "port", fallback=5080)
    app.run(host="0.0.0.0", port=port, threaded=True)
