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

#### Sonstiges
- [x] GitHub Release erstellen (v1.0.0)
