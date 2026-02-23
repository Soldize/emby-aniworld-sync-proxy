#!/usr/bin/env python3
"""
AniWorld Sync Service
Syncs anime data from API + Metadata servers into .strm/.nfo files for Emby.
"""

import configparser
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

# Config
CONFIG_PATH = os.environ.get("ANIWORLD_CONFIG", "/etc/aniworld/config.ini")
config = configparser.ConfigParser()
config.read(CONFIG_PATH)

API_PORT = config.getint("api", "port", fallback=5080)
META_PORT = config.getint("metadata", "port", fallback=5090)
PROXY_PORT = config.getint("proxy", "port", fallback=5081)
MEDIA_PATH = config.get("sync", "media_path", fallback="/media/aniworld")

API_BASE = f"http://localhost:{API_PORT}"
META_BASE = f"http://localhost:{META_PORT}"
PROXY_BASE = f"http://localhost:{PROXY_PORT}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("aniworld-sync")


def safe_filename(name):
    """Remove/replace characters not safe for filenames."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip('. ')
    return name


def pretty_xml(elem):
    """Convert ElementTree element to pretty-printed XML string."""
    rough = ET.tostring(elem, encoding='unicode')
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ", encoding=None)


def fetch_all_anime():
    """Fetch all anime from API server."""
    log.info("Fetching anime list from API server...")
    try:
        resp = requests.get(f"{API_BASE}/api/anime", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        log.info(f"Got {len(data)} anime")
        return data
    except Exception as e:
        log.error(f"Failed to fetch anime list: {e}")
        return []


def fetch_anime_detail(slug):
    """Fetch anime detail (seasons, hasMovies) from API server."""
    try:
        resp = requests.get(f"{API_BASE}/api/anime/{slug}", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Failed to fetch detail for {slug}: {e}")
        return None


def fetch_season_episodes(slug, season_num):
    """Fetch episodes for a specific season from API server."""
    try:
        resp = requests.get(f"{API_BASE}/api/anime/{slug}/season/{season_num}/episodes", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Failed to fetch episodes for {slug} S{season_num}: {e}")
        return []


def fetch_film_episodes(slug):
    """Fetch film episodes from API server."""
    try:
        resp = requests.get(f"{API_BASE}/api/anime/{slug}/films/episodes", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Failed to fetch films for {slug}: {e}")
        return []


def fetch_metadata(slug):
    """Fetch metadata from Metadata server."""
    try:
        resp = requests.get(f"{META_BASE}/metadata/{slug}", timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Failed to fetch metadata for {slug}: {e}")
        return None


def fetch_episode_metadata(slug, season):
    """Fetch per-episode metadata (titles, airdate, summary) from Metadata server."""
    try:
        resp = requests.get(f"{META_BASE}/metadata/{slug}/episodes?season={season}", timeout=15)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
        # Return as dict keyed by episode_number
        return {ep["episode_number"]: ep for ep in data}
    except Exception:
        return {}


def download_cover(url, dest_path):
    """Download cover image to dest_path if not already cached."""
    if os.path.exists(dest_path):
        return True
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, 'wb') as f:
            f.write(resp.content)
        return True
    except Exception as e:
        log.warning(f"Failed to download cover: {e}")
        return False


def write_tvshow_nfo(show_dir, anime_name, metadata):
    """Write tvshow.nfo for an anime series. Updates if missing plot."""
    nfo_path = os.path.join(show_dir, "tvshow.nfo")

    # Skip if exists and already has a plot
    if os.path.exists(nfo_path):
        try:
            with open(nfo_path, 'r', encoding='utf-8') as f:
                if "<plot>" in f.read():
                    return
        except Exception:
            pass

    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = anime_name
    ET.SubElement(root, "sorttitle").text = anime_name

    if metadata:
        # Original title (Japanese/Romaji)
        if metadata.get("title_native"):
            ET.SubElement(root, "originaltitle").text = metadata["title_native"]
        elif metadata.get("title_romaji"):
            ET.SubElement(root, "originaltitle").text = metadata["title_romaji"]

        # Description - prefer German, fallback English
        description = metadata.get("description_de") or metadata.get("description_en") or metadata.get("description")
        if description:
            ET.SubElement(root, "plot").text = description

        if metadata.get("genres"):
            for genre in metadata["genres"]:
                ET.SubElement(root, "genre").text = genre
        if metadata.get("tags"):
            for tag in metadata["tags"]:
                tag_name = tag.get("name", tag) if isinstance(tag, dict) else tag
                ET.SubElement(root, "tag").text = tag_name
        if metadata.get("rating"):
            ET.SubElement(root, "rating").text = str(metadata["rating"])
        if metadata.get("year"):
            ET.SubElement(root, "year").text = str(metadata["year"])
        if metadata.get("studio"):
            ET.SubElement(root, "studio").text = metadata["studio"]
        if metadata.get("status"):
            ET.SubElement(root, "status").text = metadata["status"]

        # Thumbnails
        if metadata.get("cover_url"):
            ET.SubElement(root, "thumb", aspect="poster").text = metadata["cover_url"]
        if metadata.get("banner_url"):
            ET.SubElement(root, "thumb", aspect="banner").text = metadata["banner_url"]

        # External IDs
        if metadata.get("anilist_id"):
            uid = ET.SubElement(root, "uniqueid", type="anilist")
            uid.text = str(metadata["anilist_id"])
        if metadata.get("mal_id"):
            uid = ET.SubElement(root, "uniqueid", type="myanimelist")
            uid.text = str(metadata["mal_id"])
        if metadata.get("anidb_id"):
            uid = ET.SubElement(root, "uniqueid", type="anidb")
            uid.text = str(metadata["anidb_id"])

    with open(nfo_path, 'w', encoding='utf-8') as f:
        f.write(pretty_xml(root))


def write_episode_nfo(nfo_path, anime_name, season, episode, title=None, ep_meta=None):
    """Write episode .nfo file."""
    root = ET.Element("episodedetails")

    # Prefer German title from AniDB, fallback to scraped title
    ep_title = title or f"Episode {episode}"
    if ep_meta:
        ep_title = ep_meta.get("title_de") or ep_meta.get("title_en") or ep_title

    ET.SubElement(root, "title").text = ep_title
    ET.SubElement(root, "showtitle").text = anime_name
    ET.SubElement(root, "season").text = str(season)
    ET.SubElement(root, "episode").text = str(episode)

    if ep_meta:
        if ep_meta.get("summary"):
            ET.SubElement(root, "plot").text = ep_meta["summary"]
        if ep_meta.get("airdate"):
            ET.SubElement(root, "aired").text = ep_meta["airdate"]
        if ep_meta.get("thumbnail_url"):
            ET.SubElement(root, "thumb").text = ep_meta["thumbnail_url"]

    with open(nfo_path, 'w', encoding='utf-8') as f:
        f.write(pretty_xml(root))


def write_strm(strm_path, slug, season, episode):
    """Write .strm file pointing to local proxy."""
    url = f"{PROXY_BASE}/play/{slug}/{season}/{episode}"
    with open(strm_path, 'w', encoding='utf-8') as f:
        f.write(url + '\n')


def sync_anime(anime, metadata):
    """Sync one anime series: create dirs, .strm, .nfo, covers."""
    slug = anime.get("slug", "")
    name = anime.get("name", slug)
    safe_name = safe_filename(name)
    show_dir = os.path.join(MEDIA_PATH, safe_name)
    os.makedirs(show_dir, exist_ok=True)

    # Write tvshow.nfo
    write_tvshow_nfo(show_dir, name, metadata)

    # Download cover
    cover_url = None
    if metadata and metadata.get("coverUrl"):
        cover_url = metadata["coverUrl"]
    elif anime.get("coverUrl"):
        cover_url = anime["coverUrl"]

    if cover_url:
        ext = "jpg"
        if ".png" in cover_url:
            ext = "png"
        poster_path = os.path.join(show_dir, f"poster.{ext}")
        meta_cover = f"{META_BASE}/cover/{slug}"
        if not download_cover(meta_cover, poster_path):
            download_cover(cover_url, poster_path)

    # Fetch anime detail for season info
    detail = fetch_anime_detail(slug)
    if not detail:
        return 0

    ep_count = 0
    seasons = detail.get("seasons", [])
    has_movies = detail.get("hasMovies", False)

    # Regular seasons
    for s in seasons:
        season_num = s.get("number", 1)
        episodes = fetch_season_episodes(slug, season_num)
        ep_metadata = fetch_episode_metadata(slug, season_num)
        for ep in episodes:
            ep_num = ep.get("episodeNumber", 1)
            ep_title = ep.get("title", f"Episode {ep_num}")
            ep_meta = ep_metadata.get(ep_num)
            ep_count += _write_episode(show_dir, safe_name, name, slug, season_num, ep_num, ep_title, ep_meta)

    # Films (season 0)
    if has_movies:
        films = fetch_film_episodes(slug)
        ep_metadata = fetch_episode_metadata(slug, 0)
        for ep in films:
            ep_num = ep.get("episodeNumber", 1)
            ep_title = ep.get("title", f"Film {ep_num}")
            ep_meta = ep_metadata.get(ep_num)
            ep_count += _write_episode(show_dir, safe_name, name, slug, 0, ep_num, ep_title, ep_meta)

    return ep_count


def _write_episode(show_dir, safe_name, anime_name, slug, season, ep_num, ep_title, ep_meta=None):
    """Write a single .strm + .nfo episode. Returns 1 if new, 0 if already exists."""

    # Season directory
    if season == 0:
        season_dir = os.path.join(show_dir, "Specials")
    else:
        season_dir = os.path.join(show_dir, f"Season {season:02d}")
    os.makedirs(season_dir, exist_ok=True)

    # Filename: "Anime - SXXEXX - Title.strm" (max 240 chars to stay under 255 limit)
    safe_title = safe_filename(ep_title)
    if season == 0:
        base_name = f"{safe_name} - S00E{ep_num:02d} - {safe_title}"
    else:
        base_name = f"{safe_name} - S{season:02d}E{ep_num:02d} - {safe_title}"

    # Truncate if too long (255 byte limit minus .strm/.nfo extension)
    if len(base_name.encode('utf-8')) > 240:
        base_name = base_name[:237].rstrip() + "..."

    strm_path = os.path.join(season_dir, f"{base_name}.strm")
    nfo_path = os.path.join(season_dir, f"{base_name}.nfo")

    # Write .strm if missing
    if not os.path.exists(strm_path):
        write_strm(strm_path, slug, season, ep_num)

    # Write .nfo if missing OR if it has no plot and we now have metadata
    nfo_needs_update = False
    if not os.path.exists(nfo_path):
        nfo_needs_update = True
    elif ep_meta and (ep_meta.get("summary") or ep_meta.get("airdate")):
        # Check if existing nfo is missing plot
        try:
            with open(nfo_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if "<plot>" not in content:
                nfo_needs_update = True
        except Exception:
            nfo_needs_update = True

    if nfo_needs_update:
        write_episode_nfo(nfo_path, anime_name, season, ep_num, ep_title, ep_meta)
    elif os.path.exists(strm_path):
        return 0  # Both exist and nfo is up to date

    return 1


WORKERS = config.getint("sync", "workers", fallback=2)


def _sync_one(args):
    """Sync a single anime (for thread pool). Returns (slug, new_eps)."""
    i, total, anime = args
    slug = anime.get("slug", "")
    name = anime.get("name", slug)
    log.info(f"[{i}/{total}] Syncing: {name}")

    metadata = fetch_metadata(slug)
    new_eps = sync_anime(anime, metadata)
    if new_eps > 0:
        log.info(f"  [{slug}] {new_eps} new episodes written")
    return slug, new_eps


def main():
    log.info("=" * 60)
    log.info("AniWorld Sync starting")
    log.info(f"API: {API_BASE} | Metadata: {META_BASE}")
    log.info(f"Media path: {MEDIA_PATH} | Workers: {WORKERS}")
    log.info("=" * 60)

    os.makedirs(MEDIA_PATH, exist_ok=True)
    start_time = time.time()

    anime_list = fetch_all_anime()
    if not anime_list:
        log.error("No anime found, aborting sync")
        sys.exit(1)

    total = len(anime_list)
    total_new = 0
    tasks = [(i + 1, total, anime) for i, anime in enumerate(anime_list)]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_sync_one, t): t for t in tasks}
        for future in as_completed(futures):
            try:
                slug, new_eps = future.result()
                total_new += new_eps
            except Exception as e:
                _, _, anime = futures[future]
                log.error(f"Error syncing {anime.get('slug', '?')}: {e}")

    elapsed = time.time() - start_time
    log.info("=" * 60)
    log.info(f"Sync complete: {total} anime, {total_new} new episodes, {elapsed:.1f}s")
    log.info("=" * 60)

    # Trigger Emby Library Scan if new episodes were written
    if total_new > 0:
        _trigger_emby_library_scan()


def _trigger_emby_library_scan():
    """Trigger Emby Library Scan via API (if configured)."""
    emby_url = config.get("emby", "url", fallback=None)
    emby_key = config.get("emby", "api_key", fallback=None)
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


if __name__ == "__main__":
    main()
