#!/usr/bin/env python3
"""
AniWorld Metadata Server
Fetches anime metadata from AniList/MAL and caches locally.
Serves metadata + cover images for the Emby AniWorld plugin.

AniDB integration: fetches German episode titles + descriptions.
Set ANIDB_CLIENT env var after client registration at https://anidb.net/software/add
"""

import gzip
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request as flask_request, send_file, abort

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
import configparser as _cp
_cfg = _cp.ConfigParser()
_cfg.read(os.environ.get("ANIWORLD_CONFIG", "/etc/aniworld/config.ini"))

PORT = _cfg.getint("metadata", "port", fallback=5090)
DB_PATH = _cfg.get("metadata", "db_path", fallback="/opt/aniworld/data/metadata.db")
COVERS_DIR = _cfg.get("metadata", "covers_dir", fallback="/opt/aniworld/data/covers")
API_SERVER = f"http://localhost:{_cfg.getint('api', 'port', fallback=5080)}"

# Rate limiting
ANILIST_RPM = 85  # stay under 90
ANILIST_DELAY = 60.0 / ANILIST_RPM
JIKAN_DELAY = 0.4  # ~2.5/sec, stay under 3/sec

REFRESH_DAYS = 7
NIGHTLY_HOUR_UTC = 3

# AniDB config
# Set ANIDB_CLIENT env var after client registration.
# Leave as "REGISTER_PENDING" until then (all AniDB calls will be skipped).
ANIDB_CLIENT = _cfg.get("anidb", "client", fallback=os.environ.get("ANIDB_CLIENT", "REGISTER_PENDING"))
ANIDB_CLIENT_VER = _cfg.getint("anidb", "client_version", fallback=int(os.environ.get("ANIDB_CLIENT_VER", "1")))
ANIDB_API_URL = "http://api.anidb.net:9001/httpapi"
ANIDB_TITLES_URL = "http://anidb.net/api/anime-titles.xml.gz"
ANIDB_TITLES_PATH = _cfg.get("metadata", "anidb_titles_path", fallback="/opt/aniworld/data/anidb-titles.xml.gz")
ANIDB_DELAY = 8.0  # seconds between API requests (AniDB min: 2s, 8s for datacenter IPs)
ANIDB_NIGHTLY_HOUR_UTC = 4  # run AniDB sync 1h after AniList sync

# SOCKS5 proxy support (e.g. Cloudflare WARP in proxy mode)
WARP_PROXY = os.environ.get("WARP_PROXY", "").strip()
if not WARP_PROXY:
    WARP_PROXY = _cfg.get("proxy", "warp_socks5", fallback="").strip()
if WARP_PROXY:
    _PROXIES = {"http": WARP_PROXY, "https": WARP_PROXY}
else:
    _PROXIES = None

logging.Formatter.converter = time.localtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("metadata")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()

    # Existing metadata table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            slug TEXT PRIMARY KEY,
            anilist_id INTEGER,
            mal_id INTEGER,
            anidb_id INTEGER,
            title_romaji TEXT,
            title_english TEXT,
            title_native TEXT,
            description_en TEXT,
            description_de TEXT,
            genres TEXT,
            tags TEXT,
            rating REAL,
            cover_url_original TEXT,
            cover_cached INTEGER DEFAULT 0,
            banner_url TEXT,
            last_updated TEXT
        )
    """)

    # Add columns to existing tables (safe: no-op if already exists)
    try:
        conn.execute("ALTER TABLE metadata ADD COLUMN anidb_id INTEGER")
    except Exception:
        pass  # already exists
    try:
        conn.execute("ALTER TABLE metadata ADD COLUMN status TEXT")
    except Exception:
        pass  # already exists
    try:
        conn.execute("ALTER TABLE metadata ADD COLUMN title_de TEXT")
    except Exception:
        pass  # already exists

    # New table: per-episode metadata (from AniDB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episode_metadata (
            slug TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            title_de TEXT,
            title_en TEXT,
            title_ja TEXT,
            airdate TEXT,
            summary TEXT,
            thumbnail_url TEXT,
            last_updated TEXT NOT NULL,
            PRIMARY KEY (slug, season, episode_number)
        )
    """)

    conn.commit()
    conn.close()
    log.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# AniList GraphQL
# ---------------------------------------------------------------------------

ANILIST_URL = "https://graphql.anilist.co"
ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME) {
      id
      idMal
      title { romaji english native }
      description(asHtml: false)
      genres
      tags { name rank }
      meanScore
      status
      coverImage { extraLarge large medium }
      bannerImage
    }
  }
}
"""

_last_anilist_req = 0.0


def _anilist_rate_limit():
    global _last_anilist_req
    elapsed = time.time() - _last_anilist_req
    if elapsed < ANILIST_DELAY:
        time.sleep(ANILIST_DELAY - elapsed)
    _last_anilist_req = time.time()


def search_anilist(title: str) -> dict | None:
    """Search AniList for an anime by title. Returns best match or None."""
    _anilist_rate_limit()
    try:
        resp = requests.post(
            ANILIST_URL,
            json={"query": ANILIST_SEARCH_QUERY, "variables": {"search": title}},
            timeout=15,
        )
        if resp.status_code == 429:
            log.warning("AniList rate limited, sleeping 60s")
            time.sleep(60)
            return search_anilist(title)
        resp.raise_for_status()
        data = resp.json()
        media_list = data.get("data", {}).get("Page", {}).get("media", [])
        if not media_list:
            return None

        # Try exact match first, then best fuzzy
        title_lower = title.lower().strip()
        for m in media_list:
            titles = m.get("title", {})
            for t in [titles.get("english"), titles.get("romaji"), titles.get("native")]:
                if t and t.lower().strip() == title_lower:
                    return m

        # Return first result as best match
        return media_list[0]
    except Exception as e:
        log.error("AniList search failed for '%s': %s", title, e)
        return None


# ---------------------------------------------------------------------------
# Jikan/MAL fallback
# ---------------------------------------------------------------------------

_last_jikan_req = 0.0


def _jikan_rate_limit():
    global _last_jikan_req
    elapsed = time.time() - _last_jikan_req
    if elapsed < JIKAN_DELAY:
        time.sleep(JIKAN_DELAY - elapsed)
    _last_jikan_req = time.time()


def search_jikan(title: str) -> dict | None:
    """Search Jikan/MAL for an anime by title."""
    _jikan_rate_limit()
    try:
        resp = requests.get(
            "https://api.jikan.moe/v4/anime",
            params={"q": title, "limit": 3, "type": "tv"},
            timeout=15,
        )
        if resp.status_code == 429:
            log.warning("Jikan rate limited, sleeping 5s")
            time.sleep(5)
            return search_jikan(title)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        return data[0]
    except Exception as e:
        log.error("Jikan search failed for '%s': %s", title, e)
        return None


# ---------------------------------------------------------------------------
# Cover download
# ---------------------------------------------------------------------------

def download_cover(slug: str, url: str) -> bool:
    """Download cover image and save to covers dir."""
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        path = os.path.join(COVERS_DIR, slug + ext)
        with open(path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        log.error("Failed to download cover for %s: %s", slug, e)
        return False


def get_cover_path(slug: str) -> str | None:
    """Find cached cover file for slug."""
    for ext in [".jpg", ".png", ".webp"]:
        p = os.path.join(COVERS_DIR, slug + ext)
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Fetch AniList metadata for a single anime
# ---------------------------------------------------------------------------

def strip_html(text: str | None) -> str | None:
    if not text:
        return text
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_and_store_metadata(slug: str, title: str, conn: sqlite3.Connection) -> bool:
    """Fetch metadata from AniList (fallback: Jikan) and store in DB."""
    log.info("Fetching metadata for: %s (%s)", slug, title)

    # Try AniList
    al = search_anilist(title)
    if al:
        titles = al.get("title", {})
        cover_url = (al.get("coverImage") or {}).get("extraLarge") or \
                    (al.get("coverImage") or {}).get("large") or ""
        genres = al.get("genres", [])
        tags_raw = al.get("tags", [])
        tags = [{"name": t["name"], "rank": t["rank"]} for t in tags_raw if t.get("rank", 0) >= 50]

        cover_cached = download_cover(slug, cover_url)

        conn.execute("""
            INSERT OR REPLACE INTO metadata
            (slug, anilist_id, mal_id, title_romaji, title_english, title_native,
             description_en, description_de, genres, tags, rating,
             cover_url_original, cover_cached, banner_url, status, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            slug,
            al.get("id"),
            al.get("idMal"),
            titles.get("romaji"),
            titles.get("english"),
            titles.get("native"),
            strip_html(al.get("description")),
            None,  # no German from AniList
            json.dumps(genres),
            json.dumps(tags),
            (al.get("meanScore") or 0) / 10.0 if al.get("meanScore") else None,
            cover_url,
            1 if cover_cached else 0,
            al.get("bannerImage"),
            al.get("status"),  # FINISHED, RELEASING, NOT_YET_RELEASED, etc.
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        log.info("Stored AniList metadata for %s (id=%s)", slug, al.get("id"))
        return True

    # Fallback: Jikan
    jk = search_jikan(title)
    if jk:
        cover_url = (jk.get("images", {}).get("jpg", {}).get("large_image_url") or
                     jk.get("images", {}).get("jpg", {}).get("image_url") or "")
        genres = [g["name"] for g in jk.get("genres", [])]
        tags = [t["name"] for t in jk.get("themes", [])]

        cover_cached = download_cover(slug, cover_url)

        # Map Jikan status to AniList-style status
        jikan_status_map = {
            "Currently Airing": "RELEASING",
            "Finished Airing": "FINISHED",
            "Not yet aired": "NOT_YET_RELEASED",
        }
        jikan_status = jikan_status_map.get(jk.get("status"), jk.get("status"))

        conn.execute("""
            INSERT OR REPLACE INTO metadata
            (slug, anilist_id, mal_id, title_romaji, title_english, title_native,
             description_en, description_de, genres, tags, rating,
             cover_url_original, cover_cached, banner_url, status, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            slug,
            None,
            jk.get("mal_id"),
            jk.get("title"),
            jk.get("title_english"),
            jk.get("title_japanese"),
            jk.get("synopsis"),
            None,
            json.dumps(genres),
            json.dumps(tags),
            jk.get("score"),
            cover_url,
            1 if cover_cached else 0,
            None,
            jikan_status,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        log.info("Stored Jikan metadata for %s (mal_id=%s)", slug, jk.get("mal_id"))
        return True

    # Fallback 2: AniDB
    if ANIDB_CLIENT != "REGISTER_PENDING":
        anidb_id = find_anidb_id(slug, title, None)
        if anidb_id:
            log.info("AniList+Jikan failed, trying AniDB for %s (aid=%s)", slug, anidb_id)
            time.sleep(ANIDB_DELAY)
            data = fetch_anidb_anime(anidb_id)
            if data and data != "BANNED":
                conn.execute("""
                    INSERT OR REPLACE INTO metadata
                    (slug, anilist_id, mal_id, anidb_id, title_romaji, title_english, title_native,
                     description_en, description_de, genres, tags, rating,
                     cover_url_original, cover_cached, banner_url, status, title_de, last_updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    slug,
                    None, None, anidb_id,
                    title, title, None,  # use aniworld title as romaji+english fallback
                    data.get("description"),  # AniDB description (often German)
                    data.get("description"),
                    json.dumps([]),  # no genres from AniDB API
                    json.dumps([]),
                    None,  # no rating
                    None, 0, None,  # no cover
                    None,  # unknown status
                    data.get("title_de"),
                    datetime.now(timezone.utc).isoformat(),
                ))
                conn.commit()
                log.info("Stored AniDB metadata for %s (aid=%s, title_de=%s)", slug, anidb_id, data.get("title_de"))
                return True

    log.warning("No metadata found for %s (%s)", slug, title)
    return False


# ---------------------------------------------------------------------------
# AniDB: titles.xml + HTTP API
# ---------------------------------------------------------------------------

_anidb_titles_cache: dict | None = None  # normalized_title → AID
_anidb_titles_loaded: float = 0.0


def _normalize_title(title: str) -> str:
    """Normalize title for fuzzy comparison (lowercase, alphanumeric only)."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


ANIDB_HEADERS = {"User-Agent": f"embyaniworld/{ANIDB_CLIENT_VER}"}


def download_anidb_titles() -> bool:
    """Download AniDB anime-titles.xml.gz (cached for 24h)."""
    path = Path(ANIDB_TITLES_PATH)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < 86400:
            return True  # fresh enough
    log.info("Downloading AniDB anime-titles.xml.gz...")
    try:
        resp = requests.get(ANIDB_TITLES_URL, headers=ANIDB_HEADERS, timeout=60, proxies=_PROXIES)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        log.info("AniDB titles downloaded (%d bytes)", len(resp.content))
        return True
    except Exception as e:
        log.error("Failed to download AniDB titles: %s", e)
        return False


def _load_anidb_titles() -> dict:
    """Load AniDB titles into memory as normalized_title → AID map (cached 24h)."""
    global _anidb_titles_cache, _anidb_titles_loaded
    now = time.time()
    if _anidb_titles_cache is not None and (now - _anidb_titles_loaded) < 86400:
        return _anidb_titles_cache

    path = Path(ANIDB_TITLES_PATH)
    if not path.exists():
        if not download_anidb_titles():
            _anidb_titles_cache = {}
            return {}

    try:
        with gzip.open(str(path), "rb") as f:
            tree = ET.parse(f)
        root = tree.getroot()

        # Build: normalized_title → AID  (first match wins for collisions)
        titles_map: dict[str, int] = {}
        for anime_el in root.findall("anime"):
            try:
                aid = int(anime_el.get("aid", 0))
            except ValueError:
                continue
            for title_el in anime_el.findall("title"):
                text = title_el.text or ""
                norm = _normalize_title(text)
                if norm and norm not in titles_map:
                    titles_map[norm] = aid

        _anidb_titles_cache = titles_map
        _anidb_titles_loaded = now
        log.info("Loaded %d AniDB title mappings", len(titles_map))
        return titles_map
    except Exception as e:
        log.error("Failed to load AniDB titles.xml: %s", e)
        _anidb_titles_cache = {}
        return {}


def find_anidb_id(slug: str, title_english: str | None, title_romaji: str | None) -> int | None:
    """Find AniDB AID for a given anime by matching known titles."""
    titles_map = _load_anidb_titles()
    if not titles_map:
        return None

    # Build candidate titles (most specific first)
    candidates = []
    if title_english:
        candidates.append(title_english)
    if title_romaji:
        candidates.append(title_romaji)
    # Slug → title (e.g. "attack-on-titan" → "Attack On Titan")
    slug_title = " ".join(w.capitalize() for w in slug.replace("-", " ").split())
    if slug_title not in candidates:
        candidates.append(slug_title)

    for candidate in candidates:
        norm = _normalize_title(candidate)
        if norm in titles_map:
            return titles_map[norm]

    return None


def _clean_anidb_text(text: str | None) -> str | None:
    """Clean AniDB-specific markup from description/summary text."""
    if not text:
        return text
    # Replace "http://anidb.net/XX12345 [Label]" with just "Label"
    text = re.sub(r"https?://anidb\.net/\S+\s*\[([^\]]+)\]", r"\1", text)
    # Remove bare AniDB URLs
    text = re.sub(r"https?://anidb\.net/\S+", "", text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_anidb_anime(anidb_id: int) -> dict | None:
    """
    Fetch anime data from AniDB HTTP API.
    Returns dict with 'description' and 'episodes' list, or None on error.
    """
    if ANIDB_CLIENT == "REGISTER_PENDING":
        log.debug("AniDB client not registered, skipping aid=%s", anidb_id)
        return None

    url = (
        f"{ANIDB_API_URL}?request=anime"
        f"&client={ANIDB_CLIENT}&clientver={ANIDB_CLIENT_VER}"
        f"&protover=1&aid={anidb_id}"
    )
    try:
        resp = requests.get(url, headers=ANIDB_HEADERS, timeout=30, proxies=_PROXIES)
    except Exception as e:
        log.error("AniDB request failed, aid=%s: %s", anidb_id, e)
        return None

    if resp.status_code == 503:
        log.warning("AniDB 503 (banned/flood protection), aid=%s — aborting sync", anidb_id)
        return "BANNED"
    if not resp.ok:
        log.warning("AniDB HTTP %s, aid=%s", resp.status_code, anidb_id)
        return None

    # AniDB always sends gzip; requests may decompress automatically
    content = resp.content
    try:
        content = gzip.decompress(content)
    except Exception:
        pass  # already decompressed or not gzip

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.error("AniDB XML parse error, aid=%s: %s", anidb_id, e)
        return None

    # AniDB returns <error> for invalid AIDs or banned clients
    if root.tag == "error":
        err_text = root.text or ""
        if "banned" in err_text.lower():
            log.warning("AniDB banned for aid=%s — aborting sync", anidb_id)
            return "BANNED"
        else:
            log.warning("AniDB error for aid=%s: %s", anidb_id, err_text)
        return None

    # ── German anime title ─────────────────────────────────────────────────
    XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
    title_de = None
    titles_el = root.find("titles")
    if titles_el is not None:
        for title_el in titles_el.findall("title"):
            lang = title_el.get(XML_LANG, "")
            if lang == "de" and title_el.text:
                title_de = title_el.text.strip()
                break

    # ── Anime description ──────────────────────────────────────────────────
    desc_el = root.find("description")
    description = _clean_anidb_text(desc_el.text if desc_el is not None else None)

    # ── Episodes ──────────────────────────────────────────────────────────
    # AniDB episode types:  1=regular, 2=special, 3=credit, 4=trailer
    # We only want type 1 (regular episodes).
    episodes = []
    episodes_el = root.find("episodes")
    if episodes_el is not None:
        for ep_el in episodes_el.findall("episode"):
            epno_el = ep_el.find("epno")
            if epno_el is None:
                continue
            if epno_el.get("type", "1") != "1":
                continue  # skip specials, credits, trailers
            try:
                ep_num = int(epno_el.text or "0")
            except ValueError:
                continue
            if ep_num <= 0:
                continue

            # Collect titles by language
            titles: dict[str, str] = {}
            for title_el in ep_el.findall("title"):
                lang = title_el.get(XML_LANG, "")
                if lang and title_el.text:
                    titles[lang] = title_el.text.strip()

            airdate_el = ep_el.find("airdate")
            summary_el = ep_el.find("summary")

            episodes.append({
                "episode_number": ep_num,
                "title_de": titles.get("de"),
                "title_en": titles.get("en"),
                # x-jat = romanized Japanese; prefer ja if present
                "title_ja": titles.get("ja") or titles.get("x-jat"),
                "airdate": airdate_el.text if airdate_el is not None else None,
                "summary": _clean_anidb_text(
                    summary_el.text if summary_el is not None else None
                ),
            })

    episodes.sort(key=lambda e: e["episode_number"])
    return {"description": description, "title_de": title_de, "episodes": episodes}


# ---------------------------------------------------------------------------
# AniDB episode sync
# ---------------------------------------------------------------------------

def sync_anidb_episodes():
    """
    For every anime in the metadata DB:
    1. Find its AniDB AID (if not already mapped)
    2. Fetch episode data from AniDB
    3. Store German/English episode titles + summaries in episode_metadata

    Episode mapping strategy (v1):
    - AniDB episode numbers (1, 2, 3, ...) map to our Season 1.
    - Many anime on AniDB have only one entry per season already (e.g. Attack on
      Titan S1 = AID 9541 with eps 1-25). This works perfectly.
    - For anime where AniDB has ALL seasons in one entry (e.g. Fairy Tail with
      175 eps), the episode numbers still match our S1 numbering directly.
    - Multi-season chain traversal (via relatedanime sequels) is a future TODO.
    """
    if ANIDB_CLIENT == "REGISTER_PENDING":
        log.info("AniDB client not registered (ANIDB_CLIENT=REGISTER_PENDING), skipping episode sync")
        return

    log.info("Starting AniDB episode sync...")
    if not download_anidb_titles():
        log.error("AniDB titles download failed, skipping episode sync")
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT slug, title_english, title_romaji, anidb_id FROM metadata"
    ).fetchall()
    conn.close()

    synced = 0
    already_done = 0
    no_aid = 0
    errors = 0

    for row in rows:
        slug = row["slug"]
        anidb_id = row["anidb_id"]

        # Find AniDB ID if not yet mapped
        if not anidb_id:
            anidb_id = find_anidb_id(slug, row["title_english"], row["title_romaji"])
            if anidb_id:
                conn = get_db()
                conn.execute("UPDATE metadata SET anidb_id=? WHERE slug=?", (anidb_id, slug))
                conn.commit()
                conn.close()
                log.info("AniDB mapping: %s → AID %s", slug, anidb_id)

        if not anidb_id:
            log.debug("No AniDB ID found for %s", slug)
            no_aid += 1
            continue

        # Skip FINISHED anime that already have episode data
        # RELEASING/unknown anime always get re-checked for new episodes
        conn = get_db()
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM episode_metadata WHERE slug=?", (slug,)
        ).fetchone()[0]
        status_row = conn.execute(
            "SELECT status FROM metadata WHERE slug=?", (slug,)
        ).fetchone()
        conn.close()
        anime_status = status_row["status"] if status_row and "status" in status_row.keys() else None

        if existing_count > 0 and anime_status == "FINISHED":
            already_done += 1
            continue

        # Rate limit: AniDB allows max 1 request/2s
        time.sleep(ANIDB_DELAY)

        data = fetch_anidb_anime(anidb_id)
        if data == "BANNED":
            log.error("AniDB ban detected — stopping episode sync. Will retry next nightly run.")
            errors += 1
            break
        if not data:
            errors += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()

        # Update German anime title from AniDB
        if data.get("title_de"):
            conn.execute(
                "UPDATE metadata SET title_de=? WHERE slug=?",
                (data["title_de"], slug),
            )

        # Update anime description if AniDB has one and we don't have DE
        if data["description"]:
            existing_row = conn.execute(
                "SELECT description_de FROM metadata WHERE slug=?", (slug,)
            ).fetchone()
            if existing_row and not existing_row["description_de"]:
                conn.execute(
                    "UPDATE metadata SET description_de=? WHERE slug=?",
                    (data["description"], slug),
                )

        # Store episode metadata (season=1 for all AniDB episodes, see strategy note above)
        ep_count = 0
        for ep in data["episodes"]:
            conn.execute("""
                INSERT OR REPLACE INTO episode_metadata
                (slug, season, episode_number, title_de, title_en, title_ja,
                 airdate, summary, thumbnail_url, last_updated)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, NULL, ?)
            """, (
                slug,
                ep["episode_number"],
                ep["title_de"],
                ep["title_en"],
                ep["title_ja"],
                ep["airdate"],
                ep["summary"],
                now,
            ))
            ep_count += 1

        conn.commit()
        conn.close()

        if ep_count > 0:
            log.info("Stored %d episodes for %s (AID %s)", ep_count, slug, anidb_id)
            synced += 1
        else:
            log.debug("AniDB returned no episodes for %s (AID %s)", slug, anidb_id)

    log.info(
        "AniDB sync done: synced=%d, already_done=%d, no_aid=%d, errors=%d",
        synced, already_done, no_aid, errors,
    )


# ---------------------------------------------------------------------------
# AniList metadata sync
# ---------------------------------------------------------------------------

def get_all_anime_from_api() -> list[dict]:
    """Fetch all anime from the aniworld API server."""
    all_anime = []
    letters = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for letter in letters:
        try:
            resp = requests.get(
                f"{API_SERVER}/api/anime",
                params={"letter": letter},
                timeout=30,
            )
            resp.raise_for_status()
            entries = resp.json()
            all_anime.extend(entries)
        except Exception as e:
            log.error("Failed to fetch anime for letter %s: %s", letter, e)
    log.info("Fetched %d anime from API server", len(all_anime))
    return all_anime


_meta_sync_running = False
_meta_sync_progress = {"total": 0, "done": 0, "fetched": 0, "skipped": 0, "errors": 0}


def sync_metadata():
    """Full AniList sync: fetch anime list, update missing/stale metadata, cleanup orphans."""
    global _meta_sync_running, _meta_sync_progress
    _meta_sync_running = True
    _meta_sync_progress = {"total": 0, "done": 0, "fetched": 0, "skipped": 0, "errors": 0}

    log.info("Starting AniList metadata sync...")
    anime_list = get_all_anime_from_api()
    if not anime_list:
        log.warning("No anime from API server, skipping sync")
        _meta_sync_running = False
        return

    api_slugs = {a["slug"] for a in anime_list}
    conn = get_db()

    existing = {}
    for row in conn.execute("SELECT slug, last_updated, status FROM metadata"):
        existing[row["slug"]] = {"last_updated": row["last_updated"], "status": row["status"] if "status" in row.keys() else None}

    now = datetime.now(timezone.utc)
    fetched = 0
    skipped = 0
    errors = 0
    _meta_sync_progress["total"] = len(anime_list)

    for anime in anime_list:
        slug = anime["slug"]
        title = anime["title"]

        # Skip if recently updated AND status is already known
        # RELEASING anime: refresh after 1 day, FINISHED: after REFRESH_DAYS
        if slug in existing and existing[slug]["last_updated"]:
            has_status = existing[slug]["status"] is not None
            is_releasing = existing[slug]["status"] in ("RELEASING", "NOT_YET_RELEASED", None)
            max_age = 1 if is_releasing else REFRESH_DAYS
            try:
                last = datetime.fromisoformat(existing[slug]["last_updated"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last).days < max_age and has_status:
                    skipped += 1
                    _meta_sync_progress["done"] = fetched + skipped + errors
                    _meta_sync_progress["skipped"] = skipped
                    continue
            except (ValueError, TypeError):
                pass

        if fetch_and_store_metadata(slug, title, conn):
            fetched += 1
        else:
            errors += 1

        _meta_sync_progress["done"] = fetched + skipped + errors
        _meta_sync_progress["fetched"] = fetched
        _meta_sync_progress["skipped"] = skipped
        _meta_sync_progress["errors"] = errors

    # Cleanup: remove anime no longer in API server
    orphans = set(existing.keys()) - api_slugs
    for slug in orphans:
        conn.execute("DELETE FROM metadata WHERE slug=?", (slug,))
        conn.execute("DELETE FROM episode_metadata WHERE slug=?", (slug,))
        cover = get_cover_path(slug)
        if cover:
            try:
                os.remove(cover)
            except OSError:
                pass
        log.info("Removed orphan: %s", slug)
    if orphans:
        conn.commit()

    conn.close()
    _meta_sync_running = False
    log.info(
        "AniList sync done: fetched=%d, skipped=%d, errors=%d, orphans=%d",
        fetched, skipped, errors, len(orphans),
    )


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

def _nightly_job():
    """
    Nightly schedule:
    - 03:00 UTC: AniList metadata sync
    - 04:00 UTC: AniDB episode sync (runs after AniList)
    """
    while True:
        now = datetime.now(timezone.utc)
        # Next AniList sync at NIGHTLY_HOUR_UTC
        target_anilist = now.replace(
            hour=NIGHTLY_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        if target_anilist <= now:
            target_anilist += timedelta(days=1)

        wait = (target_anilist - now).total_seconds()
        log.info(
            "Next AniList sync at %s UTC (in %.0f min)",
            target_anilist.strftime("%H:%M"), wait / 60,
        )
        time.sleep(wait)

        # AniList sync
        try:
            sync_metadata()
        except Exception as e:
            log.error("Nightly AniList sync failed: %s", e)

        # AniDB sync follows 30s later (same night, no need to wait until 04:00)
        time.sleep(30)
        try:
            sync_anidb_episodes()
        except Exception as e:
            log.error("Nightly AniDB sync failed: %s", e)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/metadata/<slug>")
def get_metadata(slug):
    conn = get_db()
    row = conn.execute("SELECT * FROM metadata WHERE slug=?", (slug,)).fetchone()
    conn.close()
    if not row:
        abort(404)

    cover_url = None
    if row["cover_cached"]:
        cover_url = f"/cover/{slug}"
    elif row["cover_url_original"]:
        cover_url = row["cover_url_original"]

    return jsonify({
        "slug":               row["slug"],
        "anilist_id":         row["anilist_id"],
        "mal_id":             row["mal_id"],
        "anidb_id":           row["anidb_id"],
        "title_romaji":       row["title_romaji"],
        "title_english":      row["title_english"],
        "title_native":       row["title_native"],
        "title_de":           row["title_de"] if "title_de" in row.keys() else None,
        "description_en":     row["description_en"],
        "description_de":     row["description_de"],
        "genres":             json.loads(row["genres"]) if row["genres"] else [],
        "tags":               json.loads(row["tags"]) if row["tags"] else [],
        "rating":             row["rating"],
        "cover_url":          cover_url,
        "cover_url_original": row["cover_url_original"],
        "banner_url":         row["banner_url"],
        "status":             row["status"] if "status" in row.keys() else None,
    })


@app.route("/api/status/bulk")
def get_bulk_status():
    """Returns {slug: status} for all anime with known status. Used by API-Server for incremental sync."""
    conn = get_db()
    rows = conn.execute("SELECT slug, status FROM metadata WHERE status IS NOT NULL").fetchall()
    conn.close()
    return jsonify({r["slug"]: r["status"] for r in rows})


@app.route("/metadata/<slug>/episodes")
def get_episode_metadata(slug):
    """
    Returns per-episode metadata (from AniDB) for a given slug and season.
    Query param: ?season=N (default: 1)

    Response: JSON array of episode objects with title_de, title_en, airdate, summary.
    """
    season = flask_request.args.get("season", default=1, type=int)
    conn = get_db()
    rows = conn.execute(
        """SELECT episode_number, title_de, title_en, title_ja, airdate, summary, thumbnail_url
           FROM episode_metadata
           WHERE slug=? AND season=?
           ORDER BY episode_number""",
        (slug, season),
    ).fetchall()
    conn.close()

    if not rows:
        abort(404)

    return jsonify([
        {
            "episode_number": r["episode_number"],
            "title_de":       r["title_de"],
            "title_en":       r["title_en"],
            "title_ja":       r["title_ja"],
            "airdate":        r["airdate"],
            "summary":        r["summary"],
            "thumbnail_url":  r["thumbnail_url"],
        }
        for r in rows
    ])


@app.route("/cover/<slug>")
def get_cover(slug):
    path = get_cover_path(slug)
    if not path:
        abort(404)
    if path.endswith(".png"):
        mt = "image/png"
    elif path.endswith(".webp"):
        mt = "image/webp"
    else:
        mt = "image/jpeg"
    return send_file(path, mimetype=mt)


@app.route("/status")
def get_status():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
    cached = conn.execute("SELECT COUNT(*) FROM metadata WHERE cover_cached=1").fetchone()[0]
    ep_total = conn.execute("SELECT COUNT(*) FROM episode_metadata").fetchone()[0]
    ep_slugs = conn.execute("SELECT COUNT(DISTINCT slug) FROM episode_metadata").fetchone()[0]
    anidb_mapped = conn.execute("SELECT COUNT(*) FROM metadata WHERE anidb_id IS NOT NULL").fetchone()[0]
    conn.close()
    return jsonify({
        "total_metadata":     total,
        "covers_cached":      cached,
        "covers_pending":     total - cached,
        "anidb_mapped":       anidb_mapped,
        "episode_rows":       ep_total,
        "episode_slugs":      ep_slugs,
        "anidb_client_ready": ANIDB_CLIENT != "REGISTER_PENDING",
        "syncRunning":        _meta_sync_running,
        "syncProgress":       _meta_sync_progress,
    })


@app.route("/sync", methods=["POST"])
def trigger_sync():
    """Manually trigger AniList metadata sync."""
    t = threading.Thread(target=sync_metadata, daemon=True)
    t.start()
    return jsonify({"status": "anilist sync started"})


@app.route("/anidb/sync", methods=["POST"])
def trigger_anidb_sync():
    """Manually trigger AniDB episode sync."""
    if ANIDB_CLIENT == "REGISTER_PENDING":
        return jsonify({"error": "AniDB client not registered (ANIDB_CLIENT=REGISTER_PENDING)"}), 503
    t = threading.Thread(target=sync_anidb_episodes, daemon=True)
    t.start()
    return jsonify({"status": "anidb sync started"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(COVERS_DIR, exist_ok=True)
    init_db()

    if ANIDB_CLIENT == "REGISTER_PENDING":
        log.warning(
            "AniDB client not configured! "
            "Register at https://anidb.net/software/add then set ANIDB_CLIENT env var."
        )
    else:
        log.info("AniDB client: %s v%s", ANIDB_CLIENT, ANIDB_CLIENT_VER)

    # Start nightly scheduler
    t = threading.Thread(target=_nightly_job, daemon=True)
    t.start()

    log.info("Metadata server starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
