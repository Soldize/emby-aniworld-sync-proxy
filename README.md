# AniWorld for Emby - All-in-One

Anime-Streaming von aniworld.to in Emby - als native TV-Show Library.

**Alles läuft lokal auf dem Emby-Server** - kein separater Server nötig.

## Features

- **Volle Emby Integration:** Auto-Play, Resume, Per-User Zugriff, Suche, Metadata
- **Web-Dashboard:** Status, Sync, Detail-Scrape, Katalog-Browser, Log-Viewer, Config Editor
- **Passwort-Schutz:** Dashboard Login mit SHA-256 Auth
- **API Server:** Scrapt aniworld.to, cached Episoden + Stream-URLs
- **Metadata Server:** AniList/MAL/AniDB Metadata, Cover-Bilder, Genres, Ratings
- **Stream Proxy:** Löst Hoster-URLs on-demand auf (302 Redirect)
- **Sync Service:** Erstellt .strm/.nfo Dateien für Emby Library
- **Standalone Installer:** Eine Datei, interaktives Menü, Auto-Update von GitHub
- **Kein Plugin nötig:** Alles über Standard-Emby-Bibliothek

## Voraussetzungen

- Emby Server (4.8+)
- Python 3.10+
- Ubuntu 24.04 LTS / Debian 12+

## Installation

```bash
curl -sL https://raw.githubusercontent.com/Soldize/emby-aniworld-sync-proxy/main/install.sh -o install.sh
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
7. **Deinstallieren**
8. **Anleitung** - Schritt-für-Schritt Ersteinrichtung

Nach der Installation prüft das Script automatisch ob alle Services laufen.

## Dashboard

Nach der Installation erreichbar unter: **http://localhost:5081/**

- **📊 Dashboard:** Service-Status, Aniworld Scrape, Metadata Sync, Detail Scrape, Sync Control, Config Editor
- **🔍 Katalog:** Anime-Suche, A-Z Navigation, Detail-Ansicht mit Cover + Staffeln + Episoden
- **📋 Logs:** Live-Logs von API/Metadata/Proxy, Filter nach Level, Auto-Refresh, farbcodiert
- **🔒 Auth:** Login erforderlich, Passwort im Dashboard änderbar, /play/* bleibt offen für Emby
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
│  │  .strm → resolve → 302       │      │
│  │  Web-UI: Status/Sync/Scrape  │      │
│  └───────────────────────────────┘      │
└─────────────────────────────────────────┘
```

## Services

| Service | Port | Beschreibung |
|---------|------|-------------|
| `aniworld-api` | 5080 | API Server (Scraping, Stream-Resolution) |
| `aniworld-metadata` | 5090 | Metadata Server (AniList/MAL/AniDB) |
| `aniworld-proxy` | 5081 | Stream Proxy + Web-Dashboard |
| `aniworld-sync.timer` | - | Täglicher Sync (03:00) |

## Ersteinrichtung

1. **Installer starten** - Passwort wird bei Installation festgelegt
2. **Dashboard öffnen:** http://localhost:5081/ (Login mit Passwort)
3. **Katalog wird automatisch gescraped** beim API-Start
4. **Detail Scrape starten** im Dashboard via "Batch Scrape" (holt Cover, Beschreibungen) - dauert ca. 2h, nur bei Erstinstallation nötig
5. **Sync starten** im Dashboard - generiert .strm/.nfo Dateien
6. **Emby Library:** Wird optional bei Installation automatisch angelegt, oder manuell (Typ: TV-Sendungen, Pfad: `/media/aniworld`)

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
| Python venv | `/opt/aniworld/venv/` |
