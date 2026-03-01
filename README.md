# AniWorld for Emby - All-in-One

Anime-Streaming von aniworld.to in Emby - als native TV-Show Library.

**Alles läuft lokal auf dem Emby-Server** - kein separater Server nötig.

## Features

- **Volle Emby Integration:** Auto-Play, Resume, Per-User Zugriff, Suche, Metadata
- **Web-Dashboard:** Status, Sync, Detail-Scrape, Katalog-Browser, Log-Viewer, Config Editor
- **Passwort-Schutz:** Dashboard Login mit SHA-256 Auth
- **API Server:** Scrapt aniworld.to, cached Episoden + Stream-URLs
- **Metadata Server:** AniList/MAL/AniDB Metadata, Cover-Bilder, Genres, Ratings
- **Stream Proxy:** HLS-Streams werden durch den Proxy geleitet (Retry bei fehlenden Segmenten, kein Bild/Ton Desync, Segment Pre-Fetch Buffer)
- **Stream-Token Auth:** Optionaler Token-Schutz für alle Stream-Endpoints (403 ohne Token)
- **Externer Zugriff:** Konfigurierbare Base-URL für Reverse-Proxy Setup (Windows/Samsung/iOS Apps)
- **Dashboard Split:** Dashboard auf separatem Port (nur localhost, Tunnel-Zugang) - Proxy-Port bleibt public
- **WARP Proxy:** Cloudflare WARP als SOCKS5 Proxy (nur für Hoster, nicht für aniworld.to)
- **WARP Health-Check:** Automatischer Check alle 15 Min, Reconnect nur wenn niemand schaut
- **Async Playwright:** Browser Pool mit echtem Parallelismus (kein greenlet Thread-Problem)
- **Sync Service:** Erstellt .strm/.nfo Dateien für Emby Library
- **Standalone Installer:** Eine Datei, interaktives Menü, Auto-Update von GitHub
- **Kein Plugin nötig:** Alles über Standard-Emby-Bibliothek

## Voraussetzungen

- Emby Server (4.8+)
- Python 3.10+
- Ubuntu 24.04 LTS / Debian 12+
- Chromium (wird automatisch via Playwright installiert)
- X11-Libs für Headless Chromium (`libxfixes3`, `libxcomposite1`, `libcairo2`, etc. - werden bei Installation geprüft)

## Installation

```bash
curl -sL https://raw.githubusercontent.com/Soldize/aniworld-for-emby---all-in-one/main/install.sh -o install.sh
chmod +x install.sh
sudo ./install.sh
```

Der Installer bietet ein interaktives Menü:

1. **Komplettinstallation** - Alles frisch aufsetzen (inkl. optionales Emby Library Auto-Setup)
2. **Auf Updates prüfen** - Datei-Hashes gegen GitHub vergleichen
3. **Config ändern** - Ports/Pfade anpassen
4. **Services neustarten**
5. **Status** anzeigen
6. **Passwort zurücksetzen**
7. **Backup erstellen** - DB + Config als ZIP
8. **Restore** - Backup-ZIP wiederherstellen
9. **WARP Proxy** - Cloudflare WARP installieren/verbinden/Status
10. **Deinstallieren**
11. **Anleitung** - Schritt-für-Schritt Ersteinrichtung

Nach der Installation prüft das Script automatisch ob alle Services laufen.

## Dashboard

Nach der Installation erreichbar unter: **http://localhost:5081/**

- **📊 Dashboard:** Service-Status, Hoster Health, Aniworld Scrape, Metadata Sync, Detail Scrape, Sync Control, Config Editor
- **🆕 Neu:** Zuletzt hinzugefügte Anime, Episoden, Staffeln, Filme (filterbar nach Zeitraum)
- **🔍 Katalog:** Anime-Suche, A-Z Navigation, Detail-Ansicht mit Cover + Staffeln + Episoden
- **📋 Logs:** Live-Logs von API/Metadata/Proxy, Filter nach Level, Auto-Refresh, farbcodiert
- **🔒 Auth:** Login erforderlich, Passwort im Dashboard änderbar, /play/* bleibt offen für Emby
- **💾 Backup/Restore:** DB + Config als ZIP exportieren/importieren (Dashboard + Installer)
- Buttons werden automatisch gesperrt solange ein Prozess läuft
- Responsive (Desktop, Tablet, Mobile)

## Architektur

```
┌─────────────────────────────────────────┐
│              Emby Server                │
│                                         │
│  ┌──────────┐     ┌──────────────┐      │
│  │API Server│     │Metadata Server│     │
│  │  :5080   │     │    :5090      │     │
│  └────┬─────┘     └──────┬───────┘     │
│       │                  │              │
│  ┌────┴──────────────────┴───────┐      │
│  │        Sync Service           │      │
│  │     (täglich 03:00)           │      │
│  └───────────┬───────────────────┘      │
│              │                          │
│  ┌───────────▼───────────────────┐      │
│  │    /media/aniworld/           │      │
│  │    ├── Anime Name/            │      │
│  │    │   ├── tvshow.nfo         │      │
│  │    │   ├── poster.jpg         │      │
│  │    │   └── Season 01/         │      │
│  │    │       ├── *.strm         │      │
│  │    │       └── *.nfo          │      │
│  └───────────────────────────────┘      │
│                                         │
│  ┌───────────────────────────────┐      │
│  │  Proxy + Dashboard :5081      │      │
│  │  .strm → resolve → HLS proxy  │      │
│  │  Web-UI: Status/Sync/Scrape  │      │
│  └───────────────────────────────┘      │
└─────────────────────────────────────────┘
```

## Services

| Service | Port | Beschreibung |
|---------|------|-------------|
| `aniworld-api` | 5080 | API Server (Scraping, Stream-Resolution) |
| `aniworld-metadata` | 5090 | Metadata Server (AniList/MAL/AniDB) |
| `aniworld-proxy` | 5081 | Stream Proxy (public) + Dashboard (private, wenn `dashboard_port` gesetzt) |
| `aniworld-sync.timer` | - | Täglicher Sync (03:00) |
| `warp-health-check.timer` | - | WARP Connectivity Check (alle 15 Min) |

## Ersteinrichtung

1. **Installer starten** - Passwort wird bei Installation festgelegt
2. **Dashboard öffnen:** http://localhost:5081/ (Login mit Passwort)
3. **Katalog wird automatisch gescraped** beim API-Start
4. **Detail Scrape starten** im Dashboard via "Batch Scrape" (holt Cover, Beschreibungen) - dauert ca. 2h, nur bei Erstinstallation nötig
5. **Sync starten** im Dashboard - generiert .strm/.nfo Dateien
6. **Emby Library:** Wird optional bei Installation automatisch angelegt, oder manuell (Typ: TV-Sendungen, Pfad: `/media/aniworld`)
7. **Auto Library Scan:** Wenn `[emby]` Section in Config vorhanden, wird nach jedem Sync automatisch ein Emby Library Scan getriggert

## Externer Zugriff (Windows/Samsung/iOS Apps)

Standardmäßig nutzen .strm Files `http://localhost:PORT` - das funktioniert nur für Browser und Android (Emby proxied den Stream). Für **Windows App, Samsung TV, iOS** etc. brauchen die Clients direkten Zugriff auf den Stream-Proxy.

### Setup mit Reverse-Proxy

1. **Domain/Subdomain** einrichten (z.B. `proxy.stream.example.com`)
2. **Reverse-Proxy** (nginx/Caddy/Traefik) auf den Proxy-Port weiterleiten
3. **Config anpassen** (`/etc/aniworld/config.ini`):

```ini
[proxy]
port = 5081
base_url = https://proxy.stream.example.com
stream_token = mein-geheimer-token-hier
dashboard_port = 5082
```

4. **STRM-Sync** einmal laufen lassen (damit alle .strm Files die neue URL bekommen)

### Was die Optionen machen

| Option | Beschreibung |
|--------|-------------|
| `base_url` | Externe URL für .strm + m3u8 (ohne trailing `/`) |
| `stream_token` | Auth-Token, wird als `?token=...` an alle URLs gehängt. Ohne Token → 403 |
| `dashboard_port` | Dashboard auf eigenem Port (nur 127.0.0.1). Proxy-Port bleibt public |

### Port-Aufteilung

| Port | Bind | Zugriff | Inhalt |
|------|------|---------|--------|
| 5081 (proxy) | 0.0.0.0 | Public (via Reverse-Proxy) | `/play/*`, `/stream/*`, `/health` |
| 5082 (dashboard) | 127.0.0.1 | Nur lokal/Tunnel | Dashboard, Login, API, Config |

## Nützliche Befehle

```bash
# Service Status
sudo systemctl status aniworld-api

# Logs
journalctl -u aniworld-api -f

# Manueller Sync
sudo systemctl start aniworld-sync

# Installer-Menü
sudo ./install.sh

# Schnellbefehle
sudo ./install.sh status
sudo ./install.sh update
```

## Pfade

| Was | Pfad |
|-----|------|
| Daten (DB, Cover) | `/opt/aniworld/data/` |
| Media (.strm/.nfo) | `/media/aniworld/` |
| Config | `/etc/aniworld/config.ini` |

## Konfiguration

Die Config (`/etc/aniworld/config.ini`) enthält folgende Sektionen:

| Sektion | Beschreibung |
|---------|-------------|
| `[api]` | API Server Port, DB-Pfad |
| `[metadata]` | Metadata Server Port, DB-Pfad, Covers |
| `[anidb]` | AniDB Client Name + Version (optional, für Episodentitel) |
| `[proxy]` | Proxy Port, Dashboard Port, Base-URL, Stream-Token, WARP, Pre-Fetch |
| `[sync]` | Media-Pfad für .strm/.nfo |
| `[preferences]` | Sprache, Hoster-Präferenz |
| `[emby]` | Emby URL + API-Key für Auto Library Scan (optional) |
| Python venv | `/opt/aniworld/venv/` |
