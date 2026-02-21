#!/bin/bash
# =========================================
# AniWorld for Emby - All-in-One Installer
# API Server + Metadata Server + Proxy/Dashboard + Sync
# Für Ubuntu 24.04 LTS / Debian 12+
# =========================================
set -e

INSTALL_DIR="/opt/aniworld"
DATA_DIR="/opt/aniworld/data"
CONFIG_DIR="/etc/aniworld"
MEDIA_DIR="/media/aniworld"
GITHUB_REPO="Soldize/emby-aniworld-sync-proxy"
GITHUB_RAW="https://raw.githubusercontent.com/$GITHUB_REPO/main"
GITHUB_API="https://api.github.com/repos/$GITHUB_REPO"
VERSION_FILE="$INSTALL_DIR/.version"
REQUIRED_FILES="api_server.py metadata_server.py proxy.py sync.py requirements.txt"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Hilfsfunktionen ────────────────────────────────────────────────

header() {
    echo ""
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN} AniWorld for Emby - Installer${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}Bitte als root ausführen (sudo ./install.sh)${NC}"
        exit 1
    fi
}

check_emby() {
    if ! id -u emby &>/dev/null; then
        echo -e "${RED}Emby User nicht gefunden. Bitte zuerst Emby Server installieren.${NC}"
        exit 1
    fi
}

load_config() {
    # Lade bestehende Config-Werte falls vorhanden
    if [ -f "$CONFIG_DIR/config.ini" ]; then
        API_PORT=$(grep -oP '(?<=^port = )\d+' "$CONFIG_DIR/config.ini" 2>/dev/null | head -1 || echo "5080")
        META_PORT=$(sed -n '/\[metadata\]/,/\[/{/^port/s/.*= //p}' "$CONFIG_DIR/config.ini" 2>/dev/null || echo "5090")
        PROXY_PORT=$(sed -n '/\[proxy\]/,/\[/{/^port/s/.*= //p}' "$CONFIG_DIR/config.ini" 2>/dev/null || echo "5081")
        MEDIA_PATH=$(sed -n '/\[sync\]/,/\[/{/^media_path/s/.*= //p}' "$CONFIG_DIR/config.ini" 2>/dev/null || echo "$MEDIA_DIR")
    fi
    API_PORT=${API_PORT:-5080}
    META_PORT=${META_PORT:-5090}
    PROXY_PORT=${PROXY_PORT:-5081}
    MEDIA_PATH=${MEDIA_PATH:-$MEDIA_DIR}
}

status_check() {
    echo -e "${YELLOW}Service Status:${NC}"
    for svc in aniworld-api aniworld-metadata aniworld-proxy; do
        if systemctl is-active --quiet $svc 2>/dev/null; then
            PORT=""
            case $svc in
                aniworld-api) PORT=":$API_PORT" ;;
                aniworld-metadata) PORT=":$META_PORT" ;;
                aniworld-proxy) PORT=":$PROXY_PORT" ;;
            esac
            echo -e "  ${GREEN}✅ $svc${NC} ${CYAN}$PORT${NC}"
        else
            echo -e "  ${RED}❌ $svc (nicht aktiv)${NC}"
        fi
    done
    if systemctl is-active --quiet aniworld-sync.timer 2>/dev/null; then
        echo -e "  ${GREEN}✅ aniworld-sync.timer${NC}"
    else
        echo -e "  ${RED}❌ aniworld-sync.timer (nicht aktiv)${NC}"
    fi
    echo ""
}

# ── Installations-Funktionen ───────────────────────────────────────

install_deps() {
    echo -e "${YELLOW}Installiere System-Abhängigkeiten...${NC}"
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv curl unzip > /dev/null 2>&1
    echo -e "${GREEN}✅ Abhängigkeiten installiert (Python, curl, unzip)${NC}"
}

install_files() {
    echo -e "${YELLOW}Erstelle Verzeichnisse...${NC}"
    mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/covers" "$CONFIG_DIR" "$MEDIA_PATH"

    echo -e "${YELLOW}Lade Dateien von GitHub...${NC}"
    local failed=0
    for f in $REQUIRED_FILES; do
        echo -n "  $f ... "
        if curl -sfL "$GITHUB_RAW/$f" -o "$INSTALL_DIR/$f"; then
            echo -e "${GREEN}✅${NC}"
        else
            echo -e "${RED}❌${NC}"
            failed=1
        fi
    done

    if [ "$failed" -eq 1 ]; then
        echo -e "${RED}FEHLER: Nicht alle Dateien konnten heruntergeladen werden!${NC}"
        echo -e "${RED}Prüfe deine Internetverbindung und ob das Repo existiert:${NC}"
        echo -e "${RED}https://github.com/$GITHUB_REPO${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Alle Dateien heruntergeladen${NC}"
}

install_venv() {
    echo -e "${YELLOW}Erstelle Python venv + installiere Pakete...${NC}"
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
    echo -e "${GREEN}✅ Python Pakete installiert${NC}"
}

configure() {
    echo -e "${YELLOW}Konfiguration:${NC}"
    echo "(Enter drücken für Standardwert)"
    echo ""

    read -p "API Server Port [$API_PORT]: " input
    API_PORT=${input:-$API_PORT}

    read -p "Metadata Server Port [$META_PORT]: " input
    META_PORT=${input:-$META_PORT}

    read -p "Proxy/Dashboard Port [$PROXY_PORT]: " input
    PROXY_PORT=${input:-$PROXY_PORT}

    read -p "Media Pfad für .strm Dateien [$MEDIA_PATH]: " input
    MEDIA_PATH=${input:-$MEDIA_PATH}

    mkdir -p "$MEDIA_PATH"

    cat > "$CONFIG_DIR/config.ini" << EOF
[api]
port = $API_PORT
db_path = $DATA_DIR/aniworld.db

[metadata]
port = $META_PORT
db_path = $DATA_DIR/metadata.db
covers_dir = $DATA_DIR/covers
anidb_titles_path = $DATA_DIR/anidb-titles.xml.gz

[proxy]
port = $PROXY_PORT

[sync]
media_path = $MEDIA_PATH

[preferences]
language = Deutsch
hoster = VOE
EOF
    echo -e "${GREEN}✅ Config: $CONFIG_DIR/config.ini${NC}"
}

install_services() {
    echo -e "${YELLOW}Installiere systemd Services...${NC}"

    cat > /etc/systemd/system/aniworld-api.service << EOF
[Unit]
Description=AniWorld API Server
After=network.target

[Service]
Type=simple
User=emby
Group=emby
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/api_server.py
Environment=ANIWORLD_CONFIG=$CONFIG_DIR/config.ini
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/aniworld-metadata.service << EOF
[Unit]
Description=AniWorld Metadata Server
After=network.target aniworld-api.service

[Service]
Type=simple
User=emby
Group=emby
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/metadata_server.py
Environment=ANIWORLD_CONFIG=$CONFIG_DIR/config.ini
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/aniworld-proxy.service << EOF
[Unit]
Description=AniWorld Stream Proxy + Dashboard
After=network.target aniworld-api.service

[Service]
Type=simple
User=emby
Group=emby
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/proxy.py
Environment=ANIWORLD_CONFIG=$CONFIG_DIR/config.ini
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/aniworld-sync.service << EOF
[Unit]
Description=AniWorld Sync Service
After=network.target aniworld-api.service aniworld-metadata.service

[Service]
Type=oneshot
User=emby
Group=emby
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/sync.py
Environment=ANIWORLD_CONFIG=$CONFIG_DIR/config.ini
EOF

    cat > /etc/systemd/system/aniworld-sync.timer << EOF
[Unit]
Description=AniWorld Sync Timer (daily 03:00)

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

    echo -e "${GREEN}✅ systemd Services erstellt${NC}"
}

set_permissions() {
    echo -e "${YELLOW}Setze Berechtigungen...${NC}"
    chown -R emby:emby "$INSTALL_DIR" "$DATA_DIR" "$CONFIG_DIR" "$MEDIA_PATH"
    # venv muss auch für emby lesbar sein
    chmod -R o+rX "$INSTALL_DIR/venv" 2>/dev/null || true
    echo -e "${GREEN}✅ Berechtigungen gesetzt${NC}"
}

start_services() {
    echo -e "${YELLOW}Starte Services...${NC}"
    systemctl daemon-reload
    systemctl stop aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer 2>/dev/null || true

    systemctl enable aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer
    systemctl start aniworld-api
    sleep 3
    systemctl start aniworld-metadata
    sleep 2
    systemctl start aniworld-proxy
    sleep 1
    systemctl enable --now aniworld-sync.timer

    echo ""
    sleep 3

    # Prüfen ob alles läuft
    local all_ok=true
    for svc in aniworld-api aniworld-metadata aniworld-proxy; do
        if ! systemctl is-active --quiet $svc; then
            echo -e "${RED}⚠️  $svc ist nicht gestartet!${NC}"
            echo -e "${RED}   Log: journalctl -u $svc -n 20${NC}"
            all_ok=false
        fi
    done
    if ! systemctl is-active --quiet aniworld-sync.timer; then
        echo -e "${RED}⚠️  aniworld-sync.timer ist nicht aktiv!${NC}"
        all_ok=false
    fi

    if $all_ok; then
        echo -e "${GREEN}✅ Alle Services laufen!${NC}"
    else
        echo -e "${YELLOW}⚠️  Nicht alle Services konnten gestartet werden. Siehe Logs oben.${NC}"
    fi
    echo ""
    status_check
}

restart_services() {
    echo -e "${YELLOW}Restarte Services...${NC}"
    systemctl daemon-reload
    systemctl restart aniworld-api
    sleep 3
    systemctl restart aniworld-metadata
    sleep 2
    systemctl restart aniworld-proxy
    sleep 1
    # Timer auch sicherstellen
    systemctl enable --now aniworld-sync.timer 2>/dev/null || true

    echo ""
    sleep 3

    # Prüfen ob alles läuft
    local all_ok=true
    for svc in aniworld-api aniworld-metadata aniworld-proxy; do
        if ! systemctl is-active --quiet $svc; then
            echo -e "${RED}⚠️  $svc ist nicht gestartet!${NC}"
            echo -e "${RED}   Log: journalctl -u $svc -n 20${NC}"
            all_ok=false
        fi
    done

    if $all_ok; then
        echo -e "${GREEN}✅ Alle Services laufen!${NC}"
    else
        echo -e "${YELLOW}⚠️  Nicht alle Services konnten gestartet werden. Siehe Logs oben.${NC}"
    fi
    echo ""
    status_check
}

# ── Komplettinstallation ───────────────────────────────────────────

full_install() {
    echo -e "${BOLD}Starte Komplettinstallation...${NC}"
    echo ""
    install_deps
    install_files
    install_venv
    configure
    install_services
    set_permissions

    # Version von GitHub holen und speichern
    if command -v curl &>/dev/null; then
        local ver
        ver=$(curl -s "$GITHUB_API/releases/latest" 2>/dev/null | grep -oP '"tag_name":\s*"\K[^"]+')
        if [ -z "$ver" ]; then
            ver=$(curl -s "$GITHUB_API/commits/main" 2>/dev/null | grep -oP '"sha":\s*"\K[^"]+' | head -1)
            ver="${ver:0:7}"
        fi
        [ -n "$ver" ] && save_version "$ver"
    fi

    start_services
    post_install_info
}

post_install_info() {
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN} ✅ Installation abgeschlossen!${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
    echo -e "${BOLD}${CYAN}╔═══════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}║  📊 Dashboard: http://localhost:$PROXY_PORT/         ║${NC}"
    echo -e "${BOLD}${CYAN}╚═══════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Öffne das Dashboard im Browser um alles zu steuern:"
    echo -e "  Status, Sync, Detail-Scrape und Config - alles über die Web-UI."
    echo ""
    echo -e "${BOLD}Pfade:${NC}"
    echo "  Daten:  $DATA_DIR"
    echo "  Media:  $MEDIA_PATH"
    echo "  Config: $CONFIG_DIR/config.ini"
    echo ""
    echo -e "${YELLOW}Nächste Schritte:${NC}"
    echo ""
    echo "  1. Dashboard öffnen: http://localhost:$PROXY_PORT/"
    echo ""
    echo "  2. Der API-Server scrapt den Katalog automatisch beim Start."
    echo "     Im Dashboard kannst du den Detail-Scrape manuell starten"
    echo "     (holt Cover, Beschreibungen, Staffel-Infos)."
    echo ""
    echo "  3. Danach im Dashboard 'Sync > Starten' klicken"
    echo "     um .strm Dateien zu generieren."
    echo ""
    echo "  4. In Emby: Neue Bibliothek erstellen"
    echo "     Typ:  TV-Sendungen"
    echo "     Pfad: $MEDIA_PATH"
    echo "     Name: AniWorld"
    echo ""
}

# ── Update (nur Dateien + Restart) ─────────────────────────────────

get_local_version() {
    if [ -f "$VERSION_FILE" ]; then
        cat "$VERSION_FILE"
    else
        echo "unbekannt"
    fi
}

save_version() {
    echo "$1" > "$VERSION_FILE"
    chown emby:emby "$VERSION_FILE" 2>/dev/null || true
}

check_for_updates() {
    echo -e "${BOLD}🔍 Prüfe auf Updates...${NC}"
    echo ""

    # Braucht curl oder wget
    if ! command -v curl &>/dev/null; then
        echo -e "${RED}curl nicht gefunden. Bitte installieren: apt install curl${NC}"
        return
    fi

    # Lokale Version
    local local_version
    local_version=$(get_local_version)
    echo -e "  Installierte Version: ${CYAN}$local_version${NC}"

    # GitHub: neuestes Release holen
    echo -e "  Frage GitHub nach neuester Version..."
    local api_response
    api_response=$(curl -s "$GITHUB_API/releases/latest" 2>/dev/null)

    if echo "$api_response" | grep -q '"tag_name"'; then
        local remote_version
        remote_version=$(echo "$api_response" | grep -oP '"tag_name":\s*"\K[^"]+')
        local published
        published=$(echo "$api_response" | grep -oP '"published_at":\s*"\K[^"]+' | cut -d'T' -f1)
        local body
        body=$(echo "$api_response" | grep -oP '"body":\s*"\K[^"]*' | head -1)

        echo -e "  Neueste Version:      ${CYAN}$remote_version${NC} (${published})"
        echo ""

        if [ "$local_version" = "$remote_version" ]; then
            echo -e "  ${GREEN}✅ Du bist auf dem neuesten Stand!${NC}"
            echo ""
            read -p "Drücke Enter für Menü..."
            return
        fi

        # Es gibt ein Update
        echo -e "  ${YELLOW}⬆️  Update verfügbar: $local_version → $remote_version${NC}"
        echo ""

        # Changelog anzeigen falls vorhanden
        if [ -n "$body" ]; then
            echo -e "  ${BOLD}Changelog:${NC}"
            echo "$body" | sed 's/\\r\\n/\n/g; s/\\n/\n/g' | sed 's/^/    /'
            echo ""
        fi

        read -p "Update jetzt installieren? (j/n): " do_update
        if [ "$do_update" != "j" ] && [ "$do_update" != "J" ] && [ "$do_update" != "ja" ]; then
            echo "Update übersprungen."
            return
        fi

        echo ""
        perform_github_update "$remote_version"
    else
        # Kein Release gefunden - Fallback auf Commits
        echo -e "  ${YELLOW}Kein Release gefunden, prüfe letzten Commit...${NC}"
        local commit_response
        commit_response=$(curl -s "$GITHUB_API/commits/main" 2>/dev/null)

        if echo "$commit_response" | grep -q '"sha"'; then
            local remote_sha
            remote_sha=$(echo "$commit_response" | grep -oP '"sha":\s*"\K[^"]+' | head -1)
            local remote_short="${remote_sha:0:7}"
            local commit_msg
            commit_msg=$(echo "$commit_response" | grep -oP '"message":\s*"\K[^"]*' | head -1)
            local commit_date
            commit_date=$(echo "$commit_response" | grep -oP '"date":\s*"\K[^"]+' | head -1 | cut -d'T' -f1)

            echo -e "  Letzter Commit: ${CYAN}$remote_short${NC} (${commit_date})"
            echo -e "  Message: $commit_msg"
            echo ""

            if [ "$local_version" = "$remote_short" ]; then
                echo -e "  ${GREEN}✅ Du bist auf dem neuesten Stand!${NC}"
                echo ""
                read -p "Drücke Enter für Menü..."
                return
            fi

            echo -e "  ${YELLOW}⬆️  Update verfügbar: $local_version → $remote_short${NC}"
            echo ""

            read -p "Update jetzt installieren? (j/n): " do_update
            if [ "$do_update" != "j" ] && [ "$do_update" != "J" ] && [ "$do_update" != "ja" ]; then
                echo "Update übersprungen."
                return
            fi

            echo ""
            perform_github_update "$remote_short"
        else
            echo -e "${RED}Konnte GitHub nicht erreichen. Prüfe deine Internetverbindung.${NC}"
        fi
    fi
}

perform_github_update() {
    local new_version="$1"

    # Dateien von GitHub laden (gleiche Funktion wie bei Install)
    install_files
    install_venv
    set_permissions

    # Version speichern
    save_version "$new_version"

    # Services neustarten
    restart_services

    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN} ✅ Update auf $new_version abgeschlossen!${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo -e "📊 Dashboard: http://localhost:$PROXY_PORT/"
    echo ""
    read -p "Drücke Enter für Menü..."
}

# ── Menü ───────────────────────────────────────────────────────────

show_menu() {
    echo -e "${BOLD}Was möchtest du tun?${NC}"
    echo ""
    echo -e "  ${CYAN}1)${NC} Komplettinstallation     Alles frisch installieren"
    echo -e "  ${CYAN}2)${NC} Auf Updates prüfen       GitHub nach neuer Version checken"
    echo -e "  ${CYAN}3)${NC} Config ändern             Ports/Pfade anpassen + Restart"
    echo -e "  ${CYAN}4)${NC} Services neustarten       Alle Services restarten"
    echo -e "  ${CYAN}5)${NC} Status                    Service-Status anzeigen"
    echo -e "  ${CYAN}6)${NC} Deinstallieren            Alles entfernen"
    echo -e "  ${CYAN}7)${NC} Anleitung                 Wie funktioniert das alles?"
    echo -e "  ${CYAN}0)${NC} Beenden"
    echo ""
    read -p "Auswahl [1-7/0]: " choice
}

uninstall() {
    echo -e "${RED}⚠️  Deinstallation${NC}"
    echo ""
    read -p "Wirklich alles deinstallieren? Daten werden gelöscht! (ja/nein): " confirm
    if [ "$confirm" != "ja" ]; then
        echo "Abgebrochen."
        return
    fi

    echo -e "${YELLOW}Stoppe Services...${NC}"
    systemctl stop aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer 2>/dev/null || true
    systemctl disable aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer 2>/dev/null || true
    rm -f /etc/systemd/system/aniworld-*.service /etc/systemd/system/aniworld-sync.timer
    systemctl daemon-reload

    echo -e "${YELLOW}Lösche Dateien...${NC}"
    rm -rf "$INSTALL_DIR"
    rm -rf "$CONFIG_DIR"

    read -p "Auch Media-Dateien löschen ($MEDIA_PATH)? (ja/nein): " del_media
    if [ "$del_media" = "ja" ]; then
        rm -rf "$MEDIA_PATH"
        echo -e "${GREEN}✅ Media gelöscht${NC}"
    fi

    echo -e "${GREEN}✅ Deinstallation abgeschlossen${NC}"
}

show_guide() {
    echo ""
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN} 📖 AniWorld for Emby - Anleitung${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
    echo -e "${BOLD}Was ist das?${NC}"
    echo "  AniWorld for Emby bringt Anime von aniworld.to direkt in deinen"
    echo "  Emby Media Server - als normale TV-Show Bibliothek mit Metadata,"
    echo "  Cover-Bildern, Resume/Weiterschauen und Suche."
    echo ""
    echo -e "${BOLD}Komponenten:${NC}"
    echo ""
    echo "  ${CYAN}API Server${NC} (Port $API_PORT)"
    echo "    Scrapt aniworld.to, speichert Anime-Katalog + Episoden in DB,"
    echo "    löst Stream-URLs auf. Das Herzstück."
    echo ""
    echo "  ${CYAN}Metadata Server${NC} (Port $META_PORT)"
    echo "    Holt Beschreibungen, Genres, Ratings und Cover von AniList/MAL."
    echo "    Cached alles lokal."
    echo ""
    echo "  ${CYAN}Proxy + Dashboard${NC} (Port $PROXY_PORT)"
    echo "    Der Proxy löst .strm Dateien zu echten Stream-URLs auf."
    echo "    Das Dashboard ist die Web-Oberfläche zum Steuern."
    echo ""
    echo "  ${CYAN}Sync Service${NC} (täglich 03:00)"
    echo "    Generiert .strm + .nfo Dateien in $MEDIA_PATH"
    echo "    die Emby als normale TV-Show Library einliest."
    echo ""
    echo -e "${BOLD}Dashboard (http://localhost:$PROXY_PORT/)${NC}"
    echo ""
    echo "  Das Dashboard zeigt dir:"
    echo "  - ${CYAN}Status${NC} aller Services (online/offline)"
    echo "  - ${CYAN}Sync${NC} manuell starten/stoppen mit Live-Log"
    echo "  - ${CYAN}Detail Scrape${NC}:"
    echo "      🔄 Batch Scrape  - alle ungescrapten Anime auf einmal"
    echo "      🔍 Einzeln       - einen Anime-Slug eingeben und nur den scrapen"
    echo "  - ${CYAN}Config${NC} direkt im Browser bearbeiten und speichern"
    echo ""
    echo -e "${BOLD}Ersteinrichtung nach Installation:${NC}"
    echo ""
    echo "  1. Dashboard öffnen: http://localhost:$PROXY_PORT/"
    echo ""
    echo "  2. Katalog scrapen (passiert automatisch beim API-Start),"
    echo "     dann 'Detail Scrape > 🔄 Batch Scrape' klicken."
    echo "     Das holt Beschreibungen, Cover-URLs, Staffel-Infos."
    echo "     Dauert ca. 2h bei ~2300 Anime."
    echo ""
    echo "  3. 'Sync > ▶ Starten' klicken."
    echo "     Generiert .strm + .nfo Dateien für alle Anime."
    echo ""
    echo "  4. In Emby neue Bibliothek anlegen:"
    echo "     - Typ: TV-Sendungen"
    echo "     - Pfad: $MEDIA_PATH"
    echo "     - Name: AniWorld"
    echo "     - Metadaten-Downloads DEAKTIVIEREN (kommen vom Metadata Server)"
    echo ""
    echo "  5. Emby Library Scan starten - fertig! 🎉"
    echo ""
    echo -e "${BOLD}Nützliche Befehle:${NC}"
    echo ""
    echo "  Status:       sudo systemctl status aniworld-api"
    echo "  Logs:         journalctl -u aniworld-api -f"
    echo "  Restart:      sudo systemctl restart aniworld-api"
    echo "  Manueller Sync: sudo systemctl start aniworld-sync"
    echo "  Config:       nano $CONFIG_DIR/config.ini"
    echo ""
    echo -e "${BOLD}Fehlerbehebung:${NC}"
    echo ""
    echo "  Service startet nicht?"
    echo "    journalctl -u aniworld-api -n 50"
    echo ""
    echo "  Keine Anime in Emby?"
    echo "    1. Dashboard checken - sind Services online?"
    echo "    2. API Status: curl http://localhost:$API_PORT/api/status"
    echo "    3. Sind .strm Dateien da? ls $MEDIA_PATH/"
    echo "    4. Emby Library Scan nochmal starten"
    echo ""
    read -p "Drücke Enter um zurück zum Menü zu kommen..."
}

# ── Main ───────────────────────────────────────────────────────────

check_root
check_emby
load_config

# Wenn Argument übergeben, direkt ausführen
case "${1:-}" in
    install)  header; full_install; exit 0 ;;
    update)   header; check_for_updates; exit 0 ;;
    status)   header; load_config; status_check; exit 0 ;;
    *)        ;; # Menü anzeigen
esac

# Interaktives Menü
while true; do
    header
    status_check
    show_menu

    case $choice in
        1) full_install ;;
        2) check_for_updates ;;
        3) configure; install_services; set_permissions; restart_services ;;
        4) restart_services ;;
        5) status_check; read -p "Enter für Menü..." ;;
        6) uninstall ;;
        7) show_guide ;;
        0) echo "Bye! 👋"; exit 0 ;;
        *) echo -e "${RED}Ungültige Auswahl${NC}"; sleep 1 ;;
    esac
done
