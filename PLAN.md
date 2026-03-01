# PLAN.md - Emby AniWorld Sync+Proxy

## Ziel
Ersetzt das Channel-Plugin durch einen Strm-File-Ansatz:
- Volle Emby Library Integration (Auto-Play, Resume, Per-User, Search, Metadata)
- Alles läuft auf dem Emby-Server
- Installierbar via Standalone Install-Script auf jedem Emby-Server

## Architektur

```
[Sync-Service] ---> [API-Server :5080] ---> Anime/Episoden-Daten (aniworld.to)
      |         ---> [Metadata-Server :5090] ---> Cover, Beschreibungen (AniList/MAL)
      v
  /media/aniworld/
  ├── Anime Name/
  │   ├── tvshow.nfo
  │   ├── poster.jpg
  │   ├── Season 01/
  │   │   ├── Anime - S01E01 - Titel.strm
  │   │   ├── Anime - S01E01 - Titel.nfo
  │   │   └── ...

[User klickt Play]
  .strm → http://localhost:5081/play/slug/season/episode
  → Proxy-Server → API-Server resolve → 302 Redirect → Hoster-Stream

[Dashboard :5081]
  → Web-UI zum Steuern (Status, Sync, Detail-Scrape, Config)
```

## Komponenten

### 1. API-Server (Python/Flask, Port 5080)
- Scrapt aniworld.to, cached Anime-Katalog + Episoden in SQLite
- Stream-URL Resolution (Hoster auflösen)
- Background: Auto-Sync Katalog beim Start (nur Anime-Liste, kein Detail-Scrape)
- Nightly Incremental Sync (02:00 UTC): checkt alle Anime auf neue Episoden/Staffeln/Filme
- Detail-Batch-Scrape nur bei Erstinstallation + manuell via Dashboard
- systemd Service: `aniworld-api`

### 2. Metadata-Server (Python/Flask, Port 5090)
- AniList (primär) / Jikan/MAL (Fallback) / AniDB Metadata
- Cover-Bilder lokal gecacht
- Beschreibungen, Genres, Ratings
- systemd Service: `aniworld-metadata`

### 3. Proxy-Server + Dashboard (Python/FastAPI, Port 5081)
- `GET /play/{slug}/{season}/{episode}` → Stream resolve → 302 Redirect
- Web-Dashboard (`/`) mit:
  - Service-Status (API, Metadata, Proxy, Sync)
  - Sync Control (Start/Stop mit Live-Log)
  - Detail Scrape (Batch + Einzeln per Slug, Fortschrittsbalken)
  - Config Editor (direkt im Browser bearbeiten)
- Responsive Layout (Desktop, Tablet, Mobile)
- systemd Service: `aniworld-proxy`

### 4. Sync-Service (Python)
- Holt Anime-Liste + Episoden vom API-Server
- Holt Metadata vom Metadata-Server
- Schreibt .strm + .nfo + Cover
- Ordnerstruktur Emby-kompatibel (TV Show format)
- systemd Timer: täglich 03:00 (`aniworld-sync.timer`)

### 5. Config (/etc/aniworld/config.ini)
- API Port, DB-Pfad
- Metadata Port, DB-Pfad, Covers-Dir
- Proxy Port
- Media-Pfad (default: /media/aniworld)
- Sprache + Hoster Präferenz

### 6. Install-Script (Standalone)
- Einzelne Datei, lädt alles von GitHub
- Self-Update: prüft beim Start ob neue install.sh auf GitHub liegt
- Update-Check: vergleicht md5 Hashes jeder Datei gegen GitHub
- Interaktives Menü (7 Optionen):
  1. Komplettinstallation
  2. Auf Updates prüfen (Datei-Hash Vergleich gegen GitHub)
  3. Config ändern
  4. Services neustarten
  5. Status
  6. Deinstallieren
  7. Anleitung (Ersteinrichtung, Dashboard, Fehlerbehebung)
- Post-Install Health-Check (Services + API + Dashboard erreichbar)
- CLI-Shortcuts: `./install.sh install|update|status`

## Status

### ✅ Fertig
- [x] API-Server (Katalog-Scraping, Detail-Scraping, Stream-Resolution)
- [x] Incremental Sync: checkt ALLE Anime auf neue Episoden/Staffeln/Filme (kein 23h-Filter)
- [x] Background-Loop: nur Katalog-Sync, kein auto Detail-Batch (nur bei Erstinstall + manuell)
- [x] Metadata-Server (AniList/MAL/AniDB, Cover-Cache, Sync-Progress Tracking)
- [x] Proxy-Server (Stream-Redirect, 302 zu Hoster-CDN)
- [x] Dashboard: Service-Status (online/offline)
- [x] Dashboard: Sync Control (Start/Stop mit Live-Log)
- [x] Dashboard: Aniworld Scrape (Incremental - neue Serien/Episoden)
- [x] Dashboard: Metadata Sync mit Fortschrittsbalken + Button-Sperre
- [x] Dashboard: Detail Scrape (Batch + Einzeln per Slug) mit Fortschrittsbalken + Button-Sperre
- [x] Dashboard: Config Editor (im Browser bearbeiten)
- [x] Dashboard: Responsive (Desktop/Tablet/Mobile)
- [x] Sync-Service (.strm/.nfo Generator)
- [x] Config-System (/etc/aniworld/config.ini)
- [x] systemd Services + Timer
- [x] Standalone Install-Script (lädt von GitHub, Self-Update)
- [x] Install-Menü mit Update-Check (Datei-Hash Vergleich), Anleitung, Health-Check
- [x] DB-Migration: stream_cache Spalten automatisch hinzugefügt
- [x] Stream-Playback funktioniert (getestet: hack//sign S01E01)
- [x] Git: Gitea (meeko/) + GitHub (Soldize/)
- [x] VOE Stream-Resolution via Playwright Headless Chromium (Bot-Schutz umgehen)
- [x] Incremental Sync: überspringt FINISHED Anime (nur RELEASING/unknown checken)
- [x] Incremental Sync: Quick-Check (Episode-Count vergleichen statt blind rescrape)
- [x] Metadata-Server: `status` Feld (FINISHED/RELEASING) von AniList/Jikan
- [x] Dashboard: Sync-Ergebnis anzeigen (neue Anime, Updates, Fehler-Count, Polling)
- [x] Dashboard: Doppelklick-Schutz bei Sync-Buttons (409 wenn schon läuft)
- [x] Installer: Playwright + Chromium automatisch installiert (auch via Update-Check)
- [x] Dashboard: "🆕 Neu" Tab (zuletzt hinzugefügte Anime/Episoden/Staffeln/Filme, filterbar)
- [x] Auto Emby Library Scan nach Incremental Sync + Sync-Service (wenn [emby] Config vorhanden)
- [x] recent_changes Tabelle trackt alle Änderungen
- [x] Backup/Restore: Dashboard (ZIP Download/Upload) + Installer (Menüpunkte 7+8)
- [x] Hoster Health-Monitoring: Erfolgsrate pro Hoster im Dashboard (✅/⚠️/❌, Auto-Refresh)
- [x] AniDB Client über Config konfigurierbar ([anidb] Section, Installer fragt bei Erstinstall)
- [x] NFO-Dateien: Beschreibungen (DE/EN), Originaltitel, Tags, Status, AniList/MAL/AniDB IDs, aired
- [x] Episoden-Metadata von AniDB (title_de, summary, airdate) in episode.nfo
- [x] NFOs ohne Plot werden beim Sync automatisch aktualisiert
- [x] Fortschrittsanzeige für "Änderungen scrapen" (Live-Balken, Prozent, aktueller Anime)
- [x] NFO thumb: Cover/Banner in tvshow.nfo, Episode Thumbnails von AniDB in episode.nfo
- [x] Projekt umbenannt: aniworld-for-emby---all-in-one (GitHub + Gitea + lokal)

### 🔨 Offen / Geplant

#### Dashboard: Anime-Suche / Katalog-Browser (eigener Tab) ✅
- [x] Eigener Tab im Dashboard (📊 Dashboard | 🔍 Katalog)
- [x] Suchfeld für Anime-Name
- [x] A-Z Buchstaben-Navigation mit Anime-Anzahl
- [x] Anime-Karten mit Titel, Slug, Staffeln, Status
- [x] Detail-Ansicht: Cover, Beschreibung, Staffel-Buttons
- [x] Episoden-Liste pro Staffel + Filme

#### Dashboard: Log-Viewer (eigener Tab) ✅
- [x] Eigener Tab im Dashboard (📋 Logs)
- [x] Logs von API-Server, Metadata-Server, Proxy (via journalctl)
- [x] Filter nach Level (INFO/WARNING/ERROR)
- [x] Auto-Refresh Toggle (alle 3s)
- [x] Farbcodiert (blau=INFO, gelb=WARNING, rot=ERROR)

#### Dashboard: Auth/Passwort-Schutz ✅
- [x] Bei Installation: Passwort anlegen (Pflicht)
- [x] Dashboard Login-Seite
- [x] Im Dashboard: Einstellungs-Option zum Passwort ändern
- [x] Install-Menü: Option "Passwort zurücksetzen" (Menüpunkt 6)
- [x] Session/Token-basiert (Cookie, 24h gültig)
- [x] /play/* offen für Emby, Dashboard geschützt
- [x] SHA-256 + Salt, auth.json in /etc/aniworld/

#### Emby Library Auto-Setup ✅
- [x] Bei Installation fragen: "Library anlegen?" (optional)
- [x] Emby URL + API-Key abfragen + validieren
- [x] Library-Name wählbar (Default: AniWorld)
- [x] Prüft ob Library schon existiert
- [x] Library in Emby automatisch anlegen (via Emby API, Typ: tvshows)
- [x] Nur anlegen, NICHT aktualisieren (Hinweis auf manuellen Library-Scan)
- [x] API-Key + URL in Config speichern ([emby] Section)

#### HLS Stream Proxy ✅
- [x] Stream Proxy statt 302 Redirect (behebt Bild/Ton Desync bei HLS Streams)
- [x] m3u8 Playlists werden durch Proxy gereicht und URLs umgeschrieben
- [x] Segment-Proxy mit Retry-Logik (3 Versuche bei Fehlern)
- [x] Stream-Sessions mit 4h TTL + Auto-Cleanup
- [x] /stream/active Monitoring Endpoint
- [x] httpx async HTTP Client

#### Cloudflare WARP Proxy ✅
- [x] SOCKS5 Proxy-Support nur für Hoster (VOE/Vidmoly), NICHT für aniworld.to
- [x] Playwright mit Proxy-Arg für Headless-Browser
- [x] Config: `[proxy] warp_socks5 = socks5://127.0.0.1:40000`
- [x] Env-Variable `WARP_PROXY` als Alternative
- [x] requirements.txt: `requests[socks]` + `httpx[socks]`
- [x] Installer: WARP Installation + Konfiguration (Proxy-Modus, MASQUE)
- [x] Installer: Menüpunkt 9 (WARP installieren/verbinden/Status)
- [x] Installer: Self-Update, Versionsanzeige im Header
- [x] Dashboard: WARP Status-Karte (online/offline + Cloudflare-IP)
- [x] Dashboard: Hoster Health Live-Check (nur VOE + Vidmoly)

#### Sonstiges
- [x] GitHub Release erstellen (v1.0.0)

### 🐛 Bugfixes

#### 2026-02-25: VOE Regex + WARP
- [x] VOE `_extract_voe()` matched JWPlayer Tracking-GIF statt echte m3u8 URL
- [x] Fix: `finditer` statt `search` + Blacklist für `/jwplayer`, `.gif?`
- [x] WARP `--accept-tos` bei allen warp-cli Aufrufen (root braucht ToS-Accept)
- [x] ifconfig.me: `/ip` Endpoint + `Accept: text/plain` Header (statt HTML-Seite)

#### 2026-02-23: extract_video_url + Playwright/Chromium
- [x] `extract_video_url()` Funktionsdefinition fehlte (`def` Statement nicht vorhanden)
- [x] X11-Libs für Headless Chromium fehlten auf Emby-Server (libXfixes, libcairo2, etc.)
- [x] Playwright/Chromium war nur für root installiert, nicht für User `emby` (Service läuft als emby)
- [x] stream_cache failed_at Marks gecleert die sich durch den Bug angesammelt hatten

#### AniDB Deutsche Seriennamen ✅
- [x] Deutscher Titel aus AniDB XML extrahiert (`<title xml:lang="de">`)
- [x] In metadata DB als `title_de` gespeichert
- [x] `/metadata/{slug}` API gibt `title_de` zurück
- [x] AniDB als Fallback wenn AniList + Jikan nichts finden (Chain: AniList → Jikan → AniDB)
- [x] AniDB Requests über WARP Proxy
- [x] ANIDB_DELAY 8s (Datacenter-safe)
- [x] Ban-Erkennung: Sync bricht sofort ab statt endlos zu loopen

#### Segment Pre-Fetch Buffer ✅
- [x] Sliding Window Pre-Fetch: beim Segment-Request werden die nächsten N Segmente im Hintergrund geladen
- [x] Cache im RAM mit TTL (120s), automatisches Cleanup
- [x] Konfigurierbar: `[proxy] prefetch_segments = 5` (default 5, 0 = deaktiviert)
- [x] Cache HIT = instant Response, kein Warten beim Play-Start
- [x] Segment-Order aus m3u8 Playlist extrahiert
- [x] Thread-safe mit Locks, verhindert doppeltes Fetchen

#### Filemoon Playwright-Fallback ✅
- [x] Filemoon ist ein SPA - Regex findet nichts im HTML
- [x] Neuer `_extract_playwright_generic()` für SPA-Hoster (Filemoon etc.)
- [x] Network-Intercept (m3u8/mp4 Requests abfangen) + JS-Eval Fallback
- [x] Läuft über WARP Proxy wenn konfiguriert

#### Vidmoly Regex-Fix ✅
- [x] Vidmoly nutzt einfache Anführungszeichen (`'`) statt doppelte (`"`)
- [x] Regex akzeptiert jetzt beide: `["\']?`
- [x] `sources: [{ file: '...' }]` Pattern hinzugefügt
- [x] Vidmoly Domain gewechselt: `vidmoly.biz` → `vidmoly.net` (funktioniert über WARP)

#### DeprecationWarning Cleanup ✅
- [x] `datetime.utcnow()` → `datetime.now(tz=timezone.utc)` (23 Stellen in api_server.py)
- [x] `_parse_dt()` Helper für timezone-naive DB-Werte (assume UTC)
- [x] Dashboard Log-Viewer filtert DeprecationWarnings automatisch aus

#### Persistent Playwright Browser Pool ✅
- [x] Einzelne Browser-Instanz bleibt im Hintergrund laufen (statt pro Request starten/stoppen)
- [x] Network-Request-Intercept: m3u8/mp4 URLs werden sofort abgefangen (kein 3s Blindwait)
- [x] `threading.Event()` für schnelles URL-Found-Signal (max 8s Timeout)
- [x] JS-Eval Fallback wenn Network-Intercept nichts findet
- [x] Auto-Cleanup: Browser schließt nach 5min Idle
- [x] Crash-Recovery: bei disconnected Browser wird automatisch neuer gestartet
- [x] Erwartete Performance: ~1-1.5s statt ~4s pro Stream-Resolution
- [x] VOE: Regex permanent broken (WASM-Obfuskation seit Feb 2026), Playwright-only

#### Async Playwright Pool (ersetzt greenlet-basiertes Pool) ✅
- [x] playwright.async_api statt sync_api (kein greenlet Thread-Problem mehr)
- [x] Dedizierter asyncio Event-Loop Thread für alle Playwright-Operationen
- [x] Echte Parallelität: Mehrere Browser-Tabs gleichzeitig (statt seriell via Lock)
- [x] Hoster auf VOE + Vidmoly reduziert (Filemoon, Doodstream, Streamtape, Speedfiles, Luluvdo, Vidoza entfernt)

#### WARP Optimierung ✅
- [x] aniworld.to Scraping geht direkt ohne WARP (kein "Host unreachable" mehr)
- [x] WARP nur noch für Hoster-Resolution (Playwright -> VOE/Vidmoly)
- [x] WARP Health-Check alle 15 Min (systemd Timer)
- [x] Reconnect nur wenn WARP unhealthy UND keine aktiven Streams laufen
- [x] Installer installiert Health-Check Timer automatisch

#### Externer Zugriff / Reverse-Proxy ✅
- [x] Konfigurierbare `base_url` für .strm Files + m3u8 URLs (statt localhost)
- [x] `stream_token` Auth für /play/ und /stream/ Endpoints (403 ohne Token)
- [x] Token wird in alle m3u8 Segment-URLs eingebettet
- [x] Dashboard auf separatem Port (nur 127.0.0.1, Tunnel-Zugang)
- [x] Proxy-Port public (0.0.0.0), Dashboard-Port private
- [x] Abwärtskompatibel (ohne Config = altes Verhalten)
- [x] Behebt: Windows App + Samsung TV App konnten keine Streams abspielen

### 🔨 Offen
