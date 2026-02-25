#!/bin/bash
# =========================================
# AniWorld for Emby - All-in-One Installer
# API Server + Metadata Server + Proxy/Dashboard + Sync
# Für Ubuntu 24.04 LTS / Debian 12+
# =========================================
# Kein set -e: Installer hat viele optionale Checks die non-zero returnen können

INSTALL_DIR="/opt/aniworld"
DATA_DIR="/opt/aniworld/data"
CONFIG_DIR="/etc/aniworld"
MEDIA_DIR="/media/aniworld"
GITHUB_REPO="Soldize/aniworld-for-emby---all-in-one"
GITHUB_RAW="https://raw.githubusercontent.com/$GITHUB_REPO/main"
GITHUB_API="https://api.github.com/repos/$GITHUB_REPO"
VERSION_FILE="$INSTALL_DIR/.version"
REQUIRED_FILES="api_server.py metadata_server.py proxy.py sync.py requirements.txt"
INSTALLER_VERSION="2026-02-25a"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MUTED='\033[0;90m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Hilfsfunktionen ────────────────────────────────────────────────

header() {
    echo ""
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN} AniWorld for Emby - Installer ${MUTED}v${INSTALLER_VERSION}${NC}"
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
        ANIDB_CLIENT=$(sed -n '/\[anidb\]/,/\[/{/^client /s/.*= //p}' "$CONFIG_DIR/config.ini" 2>/dev/null || echo "REGISTER_PENDING")
        ANIDB_CLIENT_VER=$(sed -n '/\[anidb\]/,/\[/{/^client_version/s/.*= //p}' "$CONFIG_DIR/config.ini" 2>/dev/null || echo "1")
    fi
    API_PORT=${API_PORT:-5080}
    META_PORT=${META_PORT:-5090}
    PROXY_PORT=${PROXY_PORT:-5081}
    MEDIA_PATH=${MEDIA_PATH:-$MEDIA_DIR}
    ANIDB_CLIENT=${ANIDB_CLIENT:-REGISTER_PENDING}
    ANIDB_CLIENT_VER=${ANIDB_CLIENT_VER:-1}
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
    # WARP Status
    if command -v warp-cli &>/dev/null; then
        local warp_st
        warp_st=$(warp-cli status 2>&1)
        if echo "$warp_st" | grep -qi "Status update: Connected"; then
            echo -e "  ${GREEN}✅ WARP Proxy${NC} ${CYAN}:40000${NC}"
        else
            echo -e "  ${YELLOW}⚠️  WARP Proxy (nicht verbunden)${NC}"
        fi
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

create_dirs() {
    echo -e "${YELLOW}Erstelle Verzeichnisse...${NC}"
    mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/covers" "$CONFIG_DIR" "$MEDIA_PATH"
}

download_files() {
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
        return 1
    fi
    echo -e "${GREEN}✅ Alle Dateien heruntergeladen${NC}"
}

install_venv() {
    echo -e "${YELLOW}Python Pakete prüfen...${NC}"
    python3 -m venv "$INSTALL_DIR/venv"

    # Pakete einzeln prüfen und Status anzeigen
    while IFS= read -r line; do
        # Kommentare und leere Zeilen überspringen
        line=$(echo "$line" | sed 's/#.*//' | xargs)
        [ -z "$line" ] && continue
        # Paketname extrahieren (vor >=, ==, etc.)
        pkg_name=$(echo "$line" | sed 's/[><=!].*//' | xargs)
        # Prüfen ob schon installiert
        local installed_ver=""
        installed_ver=$("$INSTALL_DIR/venv/bin/pip" show "$pkg_name" 2>/dev/null | grep -oP '(?<=^Version: ).+' || true)
        if [ -n "$installed_ver" ]; then
            echo -e "  ${GREEN}✅${NC} $pkg_name ($installed_ver)"
        else
            echo -ne "  ${YELLOW}⬇️  $pkg_name${NC} ... "
            "$INSTALL_DIR/venv/bin/pip" install -q "$line" 2>/dev/null
            installed_ver=$("$INSTALL_DIR/venv/bin/pip" show "$pkg_name" 2>/dev/null | grep -oP '(?<=^Version: ).+' || true)
            echo -e "${GREEN}$installed_ver${NC}"
        fi
    done < "$INSTALL_DIR/requirements.txt"

    # Upgrade aller Pakete auf neueste kompatible Version (leise)
    "$INSTALL_DIR/venv/bin/pip" install -q --upgrade -r "$INSTALL_DIR/requirements.txt" 2>/dev/null || true

    echo -e "${GREEN}✅ Python Pakete aktuell${NC}"
    install_playwright
}

install_playwright() {
    echo -e "${YELLOW}Installiere Playwright Chromium (für VOE Stream-Resolution)...${NC}"
    # Playwright braucht einige System-Deps für Chromium
    apt-get install -y -qq libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libxfixes3 libxcursor1 libxi6 libxtst6 libx11-xcb1 libxcb-dri3-0 libxss1 \
        libcairo2 libasound2t64 libxshmfence1 > /dev/null 2>&1 || \
    apt-get install -y -qq libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libxfixes3 libxcursor1 libxi6 libxtst6 libx11-xcb1 libxcb-dri3-0 libxss1 \
        libcairo2 libasound2 libxshmfence1 > /dev/null 2>&1 || true
    # Chromium installieren als emby User (Service läuft als emby!)
    sudo -u emby "$INSTALL_DIR/venv/bin/playwright" install chromium 2>/dev/null || \
        "$INSTALL_DIR/venv/bin/playwright" install chromium 2>/dev/null
    echo -e "${GREEN}✅ Playwright Chromium installiert${NC}"
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

    echo ""
    echo -e "${YELLOW}AniDB Client (optional):${NC}"
    echo "  Für Episodentitel auf Deutsch/Japanisch von AniDB."
    echo "  Client registrieren: https://anidb.net/software/add"
    echo "  Ohne Client werden AniDB-Daten übersprungen."
    echo ""
    ANIDB_CLIENT=${ANIDB_CLIENT:-REGISTER_PENDING}
    ANIDB_CLIENT_VER=${ANIDB_CLIENT_VER:-1}
    read -p "AniDB Client Name [$ANIDB_CLIENT]: " input
    ANIDB_CLIENT=${input:-$ANIDB_CLIENT}
    if [ "$ANIDB_CLIENT" != "REGISTER_PENDING" ]; then
        read -p "AniDB Client Version [$ANIDB_CLIENT_VER]: " input
        ANIDB_CLIENT_VER=${input:-$ANIDB_CLIENT_VER}
    fi

    cat > "$CONFIG_DIR/config.ini" << EOF
[api]
port = $API_PORT
db_path = $DATA_DIR/aniworld.db

[metadata]
port = $META_PORT
db_path = $DATA_DIR/metadata.db
covers_dir = $DATA_DIR/covers
anidb_titles_path = $DATA_DIR/anidb-titles.xml.gz

[anidb]
client = $ANIDB_CLIENT
client_version = $ANIDB_CLIENT_VER

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

set_dashboard_password() {
    echo ""
    echo -e "${YELLOW}Dashboard Passwort-Schutz:${NC}"
    echo ""
    while true; do
        read -sp "Dashboard Passwort festlegen (mind. 4 Zeichen): " pw1
        echo ""
        if [ ${#pw1} -lt 4 ]; then
            echo -e "${RED}Passwort muss mindestens 4 Zeichen haben!${NC}"
            continue
        fi
        read -sp "Passwort bestätigen: " pw2
        echo ""
        if [ "$pw1" != "$pw2" ]; then
            echo -e "${RED}Passwörter stimmen nicht überein!${NC}"
            continue
        fi
        break
    done

    local auth_json
    auth_json=$(python3 -c "
import hashlib, secrets, json
salt = secrets.token_hex(16)
hashed = hashlib.sha256((salt + '${pw1}').encode()).hexdigest()
print(json.dumps({'salt': salt, 'hash': hashed}))
")
    echo "$auth_json" > "$CONFIG_DIR/auth.json"
    chmod 600 "$CONFIG_DIR/auth.json"
    chown emby:emby "$CONFIG_DIR/auth.json"
    echo -e "${GREEN}✅ Dashboard Passwort gesetzt${NC}"
}

reset_dashboard_password() {
    echo ""
    echo -e "${YELLOW}Dashboard Passwort zurücksetzen:${NC}"
    if [ ! -f "$CONFIG_DIR/auth.json" ]; then
        echo -e "  Kein Passwort konfiguriert."
    fi
    set_dashboard_password
    systemctl restart aniworld-proxy 2>/dev/null || true
    echo -e "${GREEN}✅ Passwort zurückgesetzt. Proxy neugestartet.${NC}"
    echo ""
    read -p "Drücke Enter für Menü..."
}

setup_emby_library() {
    echo ""
    echo -e "${YELLOW}Emby Library einrichten:${NC}"
    echo ""
    echo "  Soll automatisch eine Emby Library angelegt werden?"
    echo "  (Emby Server muss laufen und erreichbar sein)"
    echo ""
    read -p "Library anlegen? (j/n) [n]: " do_lib
    if [ "$do_lib" != "j" ] && [ "$do_lib" != "J" ] && [ "$do_lib" != "ja" ]; then
        echo "  Übersprungen. Du kannst die Library später manuell in Emby anlegen."
        return
    fi

    read -p "Emby Server URL [http://localhost:8096]: " emby_url
    emby_url=${emby_url:-http://localhost:8096}

    # Emby erreichbar?
    if ! curl -sf "$emby_url/emby/System/Info/Public" > /dev/null 2>&1; then
        echo -e "${RED}Emby Server nicht erreichbar unter $emby_url${NC}"
        echo "  Übersprungen. Lege die Library später manuell an."
        return
    fi

    read -p "Emby API-Key (findest du unter Emby > Einstellungen > API-Schlüssel): " emby_key
    if [ -z "$emby_key" ]; then
        echo -e "${RED}Kein API-Key angegeben. Übersprungen.${NC}"
        return
    fi

    # API-Key testen
    if ! curl -sf "$emby_url/emby/Library/VirtualFolders" -H "X-Emby-Token: $emby_key" > /dev/null 2>&1; then
        echo -e "${RED}API-Key ungültig oder keine Berechtigung.${NC}"
        echo "  Übersprungen."
        return
    fi

    read -p "Library Name [AniWorld]: " lib_name
    lib_name=${lib_name:-AniWorld}

    # Prüfen ob Library schon existiert
    local existing
    existing=$(curl -s "$emby_url/emby/Library/VirtualFolders" -H "X-Emby-Token: $emby_key")
    if echo "$existing" | python3 -c "import sys,json; libs=json.load(sys.stdin); exit(0 if any(l['Name']=='$lib_name' for l in libs) else 1)" 2>/dev/null; then
        echo -e "${YELLOW}Library '$lib_name' existiert bereits!${NC}"
        echo "  Übersprungen."
        return
    fi

    # Library anlegen
    echo -e "  Erstelle Library '${lib_name}'..."
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST \
        "$emby_url/emby/Library/VirtualFolders?name=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$lib_name'))")&collectionType=tvshows&refreshLibrary=false" \
        -H "X-Emby-Token: $emby_key" \
        -H "Content-Type: application/json" \
        -d "{\"PathInfos\":[{\"Path\":\"$MEDIA_PATH\"}]}")

    local http_code
    http_code=$(echo "$response" | tail -1)

    if [ "$http_code" = "204" ] || [ "$http_code" = "200" ]; then
        echo -e "${GREEN}✅ Library '$lib_name' erstellt!${NC}"
        echo ""
        echo -e "  ${YELLOW}Wichtig:${NC} Starte nach dem ersten Sync einen Library-Scan in Emby"
        echo "  (Emby > $lib_name > ⋯ > Bibliothek aktualisieren)"

        # API-Key in Config speichern für spätere Nutzung
        if ! grep -q "\[emby\]" "$CONFIG_DIR/config.ini" 2>/dev/null; then
            cat >> "$CONFIG_DIR/config.ini" << EOF

[emby]
url = $emby_url
api_key = $emby_key
library_name = $lib_name
EOF
        fi
    else
        echo -e "${RED}Fehler beim Erstellen der Library (HTTP $http_code)${NC}"
        echo "  Lege die Library manuell in Emby an:"
        echo "  Typ: TV-Sendungen, Pfad: $MEDIA_PATH"
    fi
    echo ""
}

# ── Cloudflare WARP (optional) ─────────────────────────────────────

check_warp_status() {
    # Returns: "connected", "disconnected", "not_installed"
    if ! command -v warp-cli &>/dev/null; then
        echo "not_installed"
        return
    fi
    local status
    status=$(warp-cli status 2>&1)
    if echo "$status" | grep -qi "Status update: Connected"; then
        echo "connected"
    else
        echo "disconnected"
    fi
}

install_warp() {
    echo ""
    echo -e "${BOLD}🌐 Cloudflare WARP (SOCKS5 Proxy)${NC}"
    echo ""
    echo "  WARP verschleiert die Server-IP bei ausgehenden Requests."
    echo "  Nötig wenn Hoster (Vidmoly, Filemoon) Datacenter-IPs blocken."
    echo "  WARP läuft im Proxy-Modus (nur Port 40000, kein VPN)."
    echo ""

    local warp_status
    warp_status=$(check_warp_status)

    if [ "$warp_status" = "connected" ]; then
        local warp_ip
        warp_ip=$(curl -s --socks5-hostname 127.0.0.1:40000 https://ifconfig.me 2>/dev/null || echo "?")
        echo -e "  ${GREEN}✅ WARP ist bereits installiert und verbunden${NC}"
        echo -e "  ${GREEN}   IP über WARP: $warp_ip${NC}"
        echo ""

        # Config eintragen falls noch nicht vorhanden
        if ! grep -q "warp_socks5" "$CONFIG_DIR/config.ini" 2>/dev/null; then
            _add_warp_to_config
        fi
        return
    fi

    if [ "$warp_status" = "disconnected" ]; then
        echo -e "  ${YELLOW}⚠️  WARP ist installiert aber nicht verbunden${NC}"
        echo ""
        read -p "  WARP jetzt verbinden? (j/n) [j]: " do_connect
        if [ "$do_connect" != "n" ] && [ "$do_connect" != "N" ]; then
            _connect_warp
        fi
        return
    fi

    # Nicht installiert
    read -p "  WARP installieren? (j/n) [n]: " do_warp
    if [ "$do_warp" != "j" ] && [ "$do_warp" != "J" ] && [ "$do_warp" != "ja" ]; then
        echo -e "  ${MUTED}Übersprungen. Kann später mit ./install.sh nachinstalliert werden.${NC}"
        return
    fi

    echo ""
    echo -e "${YELLOW}Installiere Cloudflare WARP...${NC}"

    # GPG Key + Repo
    local distro
    distro=$(lsb_release -cs 2>/dev/null || echo "bookworm")
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg 2>/dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $distro main" > /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update -qq 2>/dev/null
    apt-get install -y -qq cloudflare-warp > /dev/null 2>&1

    if ! command -v warp-cli &>/dev/null; then
        echo -e "${RED}❌ WARP Installation fehlgeschlagen!${NC}"
        echo -e "  ${MUTED}Manuell installieren: https://developers.cloudflare.com/warp-client/get-started/linux/${NC}"
        return
    fi

    echo -e "${GREEN}✅ WARP installiert${NC}"

    # Registrieren + Proxy-Modus
    echo -e "${YELLOW}Registriere WARP...${NC}"
    warp-cli registration new 2>/dev/null || true
    warp-cli mode proxy 2>/dev/null || true
    warp-cli tunnel protocol set MASQUE 2>/dev/null || true

    echo -e "${GREEN}✅ WARP konfiguriert (Proxy-Modus, Port 40000, MASQUE)${NC}"
    echo ""

    echo -e "${YELLOW}⚠️  Firewall-Hinweis:${NC}"
    echo "  WARP braucht eingehende UDP-Antworten von Cloudflare."
    echo "  Falls die Verbindung fehlschlägt, diese Regeln hinzufügen:"
    echo ""
    echo "    sudo ufw allow in proto udp from 162.159.192.0/24"
    echo "    sudo ufw allow in proto udp from 162.159.198.0/24"
    echo ""
    echo "  Bei Hetzner: Auch in der Hetzner Firewall eingehend UDP erlauben."
    echo ""

    _connect_warp
}

_connect_warp() {
    echo -e "${YELLOW}Verbinde WARP...${NC}"

    # Sicherstellen: Proxy-Modus + MASQUE
    warp-cli mode proxy 2>/dev/null || true
    warp-cli tunnel protocol set MASQUE 2>/dev/null || true
    warp-cli disconnect 2>/dev/null || true
    sleep 1
    warp-cli connect 2>/dev/null || true

    # Warten auf Verbindung (max 30s)
    local tries=0
    while [ $tries -lt 30 ]; do
        local status
        status=$(warp-cli status 2>&1)
        if echo "$status" | grep -qi "Status update: Connected"; then
            break
        fi
        sleep 1
        tries=$((tries + 1))
    done

    local warp_status
    warp_status=$(check_warp_status)

    if [ "$warp_status" = "connected" ]; then
        local warp_ip
        warp_ip=$(curl -s --max-time 5 --socks5-hostname 127.0.0.1:40000 https://ifconfig.me 2>/dev/null || echo "?")
        echo -e "${GREEN}✅ WARP verbunden! IP: $warp_ip${NC}"
        _add_warp_to_config
    else
        echo -e "${RED}❌ WARP konnte nicht verbinden.${NC}"
        echo -e "  ${YELLOW}Prüfe Firewall (eingehend UDP von 162.159.192.0/24 + 162.159.198.0/24)${NC}"
        echo -e "  ${MUTED}Status: warp-cli status${NC}"
        echo -e "  ${MUTED}Logs: journalctl -u warp-svc -n 20${NC}"
        echo ""
        read -p "  Trotzdem WARP in Config eintragen (für spätere Verbindung)? (j/n) [n]: " do_anyway
        if [ "$do_anyway" = "j" ] || [ "$do_anyway" = "J" ]; then
            _add_warp_to_config
        fi
    fi
}

_add_warp_to_config() {
    if [ ! -f "$CONFIG_DIR/config.ini" ]; then
        return
    fi
    if grep -q "warp_socks5" "$CONFIG_DIR/config.ini" 2>/dev/null; then
        return  # Schon drin
    fi
    # Unter [proxy] Section eintragen
    if grep -q "^\[proxy\]" "$CONFIG_DIR/config.ini" 2>/dev/null; then
        sed -i '/^\[proxy\]/a warp_socks5 = socks5:\/\/127.0.0.1:40000' "$CONFIG_DIR/config.ini"
    else
        cat >> "$CONFIG_DIR/config.ini" << 'EOF'

[proxy]
warp_socks5 = socks5://127.0.0.1:40000
EOF
    fi
    echo -e "  ${GREEN}✅ WARP Proxy in Config eingetragen${NC}"
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
    # sudoers für Dashboard-Restart-Buttons (emby darf Services neustarten)
    echo "emby ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart aniworld-api, /usr/bin/systemctl restart aniworld-metadata, /usr/bin/systemctl restart aniworld-proxy, /usr/bin/systemctl restart aniworld-sync, /usr/bin/journalctl" \
        > /etc/sudoers.d/aniworld-restart
    chmod 440 /etc/sudoers.d/aniworld-restart
    echo -e "${GREEN}✅ Berechtigungen gesetzt${NC}"
}

start_services() {
    echo -e "${YELLOW}Starte Services...${NC}"
    systemctl daemon-reload
    systemctl stop aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer 2>/dev/null || true

    systemctl enable aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer
    systemctl start aniworld-api
    echo -e "  Warte auf API Server..."
    sleep 3
    systemctl start aniworld-metadata
    echo -e "  Warte auf Metadata Server..."
    sleep 2
    systemctl start aniworld-proxy
    echo -e "  Warte auf Proxy/Dashboard..."
    sleep 2
    systemctl enable --now aniworld-sync.timer
    echo -e "  Sync Timer aktiviert."
    sleep 2
    echo ""
}

restart_services() {
    echo -e "${YELLOW}Restarte Services...${NC}"
    systemctl daemon-reload
    systemctl restart aniworld-api
    sleep 3
    systemctl restart aniworld-metadata
    sleep 2
    systemctl restart aniworld-proxy
    sleep 2
    systemctl enable --now aniworld-sync.timer 2>/dev/null || true
    echo ""
    verify_services
}

verify_services() {
    echo -e "${YELLOW}Prüfe ob alles läuft...${NC}"
    echo ""

    local all_ok=true

    for svc in aniworld-api aniworld-metadata aniworld-proxy; do
        if systemctl is-active --quiet $svc; then
            echo -e "  ${GREEN}✅ $svc läuft${NC}"
        else
            echo -e "  ${RED}❌ $svc läuft NICHT${NC}"
            echo -e "     ${RED}→ journalctl -u $svc -n 20${NC}"
            all_ok=false
        fi
    done
    if systemctl is-active --quiet aniworld-sync.timer; then
        echo -e "  ${GREEN}✅ aniworld-sync.timer aktiv${NC}"
    else
        echo -e "  ${RED}❌ aniworld-sync.timer NICHT aktiv${NC}"
        all_ok=false
    fi

    echo ""
    if $all_ok; then
        echo -e "${GREEN}✅ Alle Services laufen!${NC}"
    else
        echo -e "${YELLOW}⚠️  Nicht alle Services konnten gestartet werden.${NC}"
    fi
    echo ""
}

# ── Komplettinstallation ───────────────────────────────────────────

full_install() {
    echo -e "${BOLD}Starte Komplettinstallation...${NC}"
    echo ""
    install_deps
    create_dirs
    download_files
    install_venv
    configure
    set_dashboard_password
    install_warp
    install_services
    set_permissions

    # Version von GitHub holen und speichern
    if command -v curl &>/dev/null; then
        local ver=""
        ver=$(curl -s "$GITHUB_API/releases/latest" 2>/dev/null | grep -oP '"tag_name":\s*"\K[^"]+' || true)
        if [ -z "$ver" ]; then
            ver=$(curl -s "$GITHUB_API/commits/main" 2>/dev/null | grep -oP '"sha":\s*"\K[^"]+' | head -1 || true)
            ver="${ver:0:7}"
        fi
        if [ -n "$ver" ]; then
            save_version "$ver"
        fi
    fi

    start_services
    setup_emby_library
    post_install_check
}

post_install_check() {
    # Services checken
    verify_services

    # API erreichbar?
    local all_ok=true
    echo -n "  API Server erreichbar ... "
    if curl -sf "http://localhost:$API_PORT/api/status" > /dev/null 2>&1; then
        echo -e "${GREEN}✅${NC}"
    else
        echo -e "${RED}❌${NC}"
        all_ok=false
    fi

    # Dashboard erreichbar?
    echo -n "  Dashboard erreichbar ... "
    if curl -sf "http://localhost:$PROXY_PORT/" > /dev/null 2>&1; then
        echo -e "${GREEN}✅${NC}"
    else
        echo -e "${RED}❌${NC}"
        all_ok=false
    fi

    # Nochmal alle Services checken für Gesamtergebnis
    for svc in aniworld-api aniworld-metadata aniworld-proxy aniworld-sync.timer; do
        if ! systemctl is-active --quiet $svc; then
            all_ok=false
        fi
    done

    echo ""

    if $all_ok; then
        echo -e "${GREEN}=========================================${NC}"
        echo -e "${GREEN} ✅ Erfolgreich installiert!${NC}"
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
    else
        echo -e "${RED}=========================================${NC}"
        echo -e "${RED} ⚠️  Installation mit Fehlern!${NC}"
        echo -e "${RED}=========================================${NC}"
        echo ""
        echo -e "  Nicht alle Services laufen korrekt."
        echo -e "  Prüfe die Logs mit: journalctl -u <service> -n 20"
        echo -e "  Oder starte nochmal: sudo ./install.sh"
    fi

    echo ""
    read -p "Drücke Enter um zum Menü zurückzukehren..."
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

    # Dateien von GitHub laden und gegen lokale vergleichen (Hash-Check)
    echo -e "  Vergleiche lokale Dateien mit GitHub..."
    echo ""

    local tmp_dir
    tmp_dir=$(mktemp -d)
    local has_updates=false
    local update_files=""

    for f in $REQUIRED_FILES; do
        if curl -sfL "$GITHUB_RAW/$f" -o "$tmp_dir/$f" 2>/dev/null; then
            local local_hash="none"
            local remote_hash=""
            if [ -f "$INSTALL_DIR/$f" ]; then
                local_hash=$(md5sum "$INSTALL_DIR/$f" 2>/dev/null | cut -d' ' -f1)
            fi
            remote_hash=$(md5sum "$tmp_dir/$f" 2>/dev/null | cut -d' ' -f1)

            if [ "$local_hash" != "$remote_hash" ]; then
                echo -e "  ${YELLOW}⬆️  $f${NC} - Update verfügbar"
                has_updates=true
                update_files="$update_files $f"
            else
                echo -e "  ${GREEN}✅ $f${NC} - aktuell"
            fi
        else
            echo -e "  ${RED}❌ $f${NC} - Download fehlgeschlagen"
        fi
    done

    echo ""

    if ! $has_updates; then
        echo -e "  ${GREEN}✅ Alle Dateien sind auf dem neuesten Stand!${NC}"
        rm -rf "$tmp_dir"
        echo ""
        read -p "Drücke Enter für Menü..."
        return
    fi

    echo -e "  ${YELLOW}Updates verfügbar für:${BOLD}$update_files${NC}"
    echo ""

    # Changelog von GitHub holen (Commits seit letztem Update)
    local installed_ver=""
    if [ -f "$INSTALL_DIR/.version" ]; then
        installed_ver=$(cat "$INSTALL_DIR/.version" 2>/dev/null | head -1)
    fi
    echo -e "${BOLD}📋 Changelog:${NC}"
    echo ""
    local commits_json
    commits_json=$(curl -s "$GITHUB_API/commits?per_page=30" 2>/dev/null)
    if [ -n "$commits_json" ]; then
        # SHA + Message extrahieren (abwechselnd sha, message Zeilen)
        local shas messages
        shas=$(echo "$commits_json" | grep -oP '"sha":\s*"\K[^"]+' | head -30)
        messages=$(echo "$commits_json" | grep -oP '"message":\s*"\K[^"]+' | head -30)
        local shown=0
        paste <(echo "$shas") <(echo "$messages") | while IFS=$'\t' read -r sha msg; do
            # Bei installierter Version stoppen
            if [ -n "$installed_ver" ] && [ "${sha:0:${#installed_ver}}" = "$installed_ver" ]; then
                break
            fi
            echo -e "  ${BLUE}•${NC} $msg"
            shown=$((shown + 1))
        done
        if [ -z "$installed_ver" ]; then
            echo -e "  ${MUTED}(letzte 10 Commits, keine installierte Version bekannt)${NC}"
        fi
    else
        echo -e "  ${MUTED}(Changelog konnte nicht geladen werden)${NC}"
    fi
    echo ""

    read -p "Update jetzt installieren? (j/n): " do_update
    if [ "$do_update" != "j" ] && [ "$do_update" != "J" ] && [ "$do_update" != "ja" ]; then
        echo "Update übersprungen."
        rm -rf "$tmp_dir"
        return
    fi

    # Dateien kopieren
    echo ""
    echo -e "${YELLOW}Aktualisiere Dateien...${NC}"
    for f in $REQUIRED_FILES; do
        if [ -f "$tmp_dir/$f" ]; then
            cp "$tmp_dir/$f" "$INSTALL_DIR/$f"
            echo -e "  ${GREEN}✅ $f${NC}"
        fi
    done
    rm -rf "$tmp_dir"

    echo -e "${GREEN}✅ Dateien aktualisiert${NC}"

    # Berechtigungen + venv (inkl. Playwright) + WARP check + Restart
    set_permissions
    install_venv

    # WARP Status prüfen (bei Update nur Status-Check, keine Neuinstallation)
    local warp_status
    warp_status=$(check_warp_status)
    if [ "$warp_status" = "connected" ]; then
        local warp_ip
        warp_ip=$(curl -s --max-time 5 --socks5-hostname 127.0.0.1:40000 https://ifconfig.me 2>/dev/null || echo "?")
        echo -e "  ${GREEN}✅ WARP aktiv (IP: $warp_ip)${NC}"
    elif [ "$warp_status" = "disconnected" ]; then
        echo -e "  ${YELLOW}⚠️  WARP installiert aber nicht verbunden${NC}"
        read -p "  WARP verbinden? (j/n) [j]: " do_warp_connect
        if [ "$do_warp_connect" != "n" ] && [ "$do_warp_connect" != "N" ]; then
            _connect_warp
        fi
    elif ! grep -q "warp_socks5" "$CONFIG_DIR/config.ini" 2>/dev/null; then
        echo ""
        echo -e "  ${YELLOW}💡 Tipp: WARP Proxy hilft gegen Hoster-IP-Blocking.${NC}"
        read -p "  WARP jetzt installieren? (j/n) [n]: " do_warp_install
        if [ "$do_warp_install" = "j" ] || [ "$do_warp_install" = "J" ]; then
            install_warp
        fi
    fi

    echo ""
    echo -e "${YELLOW}Starte Services neu...${NC}"
    systemctl daemon-reload
    systemctl restart aniworld-api
    sleep 3
    systemctl restart aniworld-metadata
    sleep 2
    systemctl restart aniworld-proxy
    sleep 2
    systemctl enable --now aniworld-sync.timer 2>/dev/null || true

    echo ""
    verify_services

    # Version speichern
    local ver=""
    ver=$(curl -s "$GITHUB_API/commits/main" 2>/dev/null | grep -oP '"sha":\s*"\K[^"]+' | head -1 || true)
    if [ -n "$ver" ]; then
        save_version "${ver:0:7}"
    fi

    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN} ✅ Update abgeschlossen!${NC}"
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
    echo -e "  ${CYAN}6)${NC} Passwort zurücksetzen     Dashboard Passwort neu setzen"
    echo -e "  ${CYAN}7)${NC} Backup erstellen          DB + Config als ZIP exportieren"
    echo -e "  ${CYAN}8)${NC} Restore                   Backup-ZIP wiederherstellen"
    echo -e "  ${CYAN}9)${NC} WARP Proxy                Cloudflare WARP installieren/prüfen"
    echo -e "  ${CYAN}10)${NC} Deinstallieren            Alles entfernen"
    echo -e "  ${CYAN}11)${NC} Anleitung                Wie funktioniert das alles?"
    echo -e "  ${CYAN}0)${NC} Beenden"
    echo ""
    read -p "Auswahl [1-11/0]: " choice
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
    echo -e "${BOLD}Ersteinrichtung:${NC}"
    echo ""
    echo "  1. Dashboard öffnen: http://localhost:$PROXY_PORT/"
    echo "  2. '📦 Batch Scrape' klicken (Details, Cover holen - ca. 2h)"
    echo "  3. '▶ Sync Starten' (.strm/.nfo Dateien generieren)"
    echo "  4. Emby Library Scan starten - fertig! 🎉"
    echo ""
    echo -e "${BOLD}Dashboard-Tabs:${NC}"
    echo ""
    echo -e "  ${CYAN}📊 Dashboard${NC}  Status, Hoster Health, Scrape, Sync, Config"
    echo -e "  ${CYAN}🆕 Neu${NC}        Zuletzt hinzugefügte Anime/Episoden/Filme"
    echo -e "  ${CYAN}🔍 Katalog${NC}    Suche, A-Z Navigation, Detail-Ansicht"
    echo -e "  ${CYAN}📋 Logs${NC}       Live-Logs aller Services (filterbar)"
    echo -e "  ${CYAN}🔧 Settings${NC}   Passwort, Backup/Restore"
    echo ""
    echo -e "${BOLD}Services:${NC}"
    echo ""
    echo -e "  ${CYAN}API Server${NC}      :$API_PORT  Scraping, Stream-Resolution (VOE via Chromium)"
    echo -e "  ${CYAN}Metadata${NC}        :$META_PORT  AniList/MAL/AniDB Metadata + Cover"
    echo -e "  ${CYAN}Proxy/Dashboard${NC} :$PROXY_PORT  Stream-Redirect + Web-UI"
    echo -e "  ${CYAN}Sync${NC}            tägl. 03:00  .strm/.nfo nach $MEDIA_PATH"
    echo ""
    echo -e "${BOLD}Config ($CONFIG_DIR/config.ini):${NC}"
    echo ""
    echo "  [emby]           Auto Library Scan nach Sync (optional)"
    echo "    url = http://localhost:8096"
    echo "    api_key = DEIN_API_KEY"
    echo ""
    echo "  [anidb]          AniDB Episodentitel (optional)"
    echo "    client = DEIN_CLIENT_NAME"
    echo "    client_version = 1"
    echo "    Registrieren: https://anidb.net/software/add"
    echo ""
    echo "  [proxy]          WARP SOCKS5 Proxy (optional)"
    echo "    warp_socks5 = socks5://127.0.0.1:40000"
    echo "    Installieren: Menü (9) oder bei Erstinstallation"
    echo ""
    echo -e "${BOLD}Befehle:${NC}"
    echo ""
    echo "  sudo ./install.sh status    Service-Status"
    echo "  sudo ./install.sh update    Auf Updates prüfen"
    echo "  journalctl -u aniworld-api -f   Live-Logs"
    echo "  nano $CONFIG_DIR/config.ini     Config bearbeiten"
    echo ""
    echo -e "${BOLD}Backup:${NC}  Menü (7) oder Dashboard > Settings > 💾"
    echo -e "${BOLD}Restore:${NC} Menü (8) oder Dashboard > Settings > 📂"
    echo ""
    read -p "Enter für Menü..."
}

# ── Backup / Restore ──────────────────────────────────────────────

create_backup() {
    echo -e "${BOLD}💾 Backup erstellen${NC}"
    echo ""

    local timestamp
    timestamp=$(date +%Y%m%d-%H%M%S)
    local backup_file="aniworld-backup-${timestamp}.zip"
    local default_path="/tmp/${backup_file}"

    read -p "Speicherort [$default_path]: " backup_path
    backup_path=${backup_path:-$default_path}

    # Wenn nur ein Verzeichnis angegeben wurde, Dateiname anhängen
    if [ -d "$backup_path" ]; then
        backup_path="${backup_path%/}/${backup_file}"
    fi

    # Prüfe ob zip installiert ist
    if ! command -v zip &>/dev/null; then
        echo -e "${YELLOW}Installiere zip...${NC}"
        apt-get install -y -qq zip > /dev/null 2>&1
    fi

    echo -e "  Sammle Dateien..."
    local files_to_backup=""

    [ -f "$CONFIG_DIR/config.ini" ] && files_to_backup="$files_to_backup $CONFIG_DIR/config.ini"
    [ -f "$CONFIG_DIR/auth.json" ] && files_to_backup="$files_to_backup $CONFIG_DIR/auth.json"
    [ -f "$DATA_DIR/aniworld.db" ] && files_to_backup="$files_to_backup $DATA_DIR/aniworld.db"
    [ -f "$DATA_DIR/metadata.db" ] && files_to_backup="$files_to_backup $DATA_DIR/metadata.db"

    if [ -z "$files_to_backup" ]; then
        echo -e "${RED}Keine Dateien zum Backup gefunden!${NC}"
        read -p "Enter für Menü..."
        return
    fi

    zip -j "$backup_path" $files_to_backup > /dev/null 2>&1

    if [ -f "$backup_path" ]; then
        local size
        size=$(du -h "$backup_path" | cut -f1)
        echo ""
        echo -e "${GREEN}✅ Backup erstellt: ${BOLD}$backup_path${NC} (${size})"
        echo ""
        echo "  Enthält:"
        for f in $files_to_backup; do
            echo -e "    ${GREEN}✅${NC} $(basename $f)"
        done
        echo ""
        echo "  Zum Kopieren: scp root@$(hostname):$backup_path ."
    else
        echo -e "${RED}Backup fehlgeschlagen!${NC}"
    fi
    echo ""
    read -p "Enter für Menü..."
}

restore_backup() {
    echo -e "${BOLD}📂 Backup wiederherstellen${NC}"
    echo ""
    echo -e "  ${YELLOW}⚠️  Dies überschreibt die aktuelle DB + Config!${NC}"
    echo ""
    read -p "Pfad zur Backup-ZIP: " backup_zip

    if [ -z "$backup_zip" ] || [ ! -f "$backup_zip" ]; then
        echo -e "${RED}Datei nicht gefunden: $backup_zip${NC}"
        read -p "Enter für Menü..."
        return
    fi

    # Prüfe ob unzip installiert ist
    if ! command -v unzip &>/dev/null; then
        echo -e "${YELLOW}Installiere unzip...${NC}"
        apt-get install -y -qq unzip > /dev/null 2>&1
    fi

    # Pre-Restore Backup
    local timestamp
    timestamp=$(date +%Y%m%d-%H%M%S)
    local pre_restore_dir="$DATA_DIR/pre-restore-${timestamp}"
    mkdir -p "$pre_restore_dir"

    echo -e "  Sichere aktuelle Daten nach ${pre_restore_dir}..."
    [ -f "$CONFIG_DIR/config.ini" ] && cp "$CONFIG_DIR/config.ini" "$pre_restore_dir/"
    [ -f "$CONFIG_DIR/auth.json" ] && cp "$CONFIG_DIR/auth.json" "$pre_restore_dir/"
    [ -f "$DATA_DIR/aniworld.db" ] && cp "$DATA_DIR/aniworld.db" "$pre_restore_dir/"
    [ -f "$DATA_DIR/metadata.db" ] && cp "$DATA_DIR/metadata.db" "$pre_restore_dir/"

    echo -e "  Stelle Backup wieder her..."
    local tmp_dir
    tmp_dir=$(mktemp -d)
    unzip -o "$backup_zip" -d "$tmp_dir" > /dev/null 2>&1

    local restored=""
    [ -f "$tmp_dir/config.ini" ] && cp "$tmp_dir/config.ini" "$CONFIG_DIR/config.ini" && restored="$restored config.ini"
    [ -f "$tmp_dir/auth.json" ] && cp "$tmp_dir/auth.json" "$CONFIG_DIR/auth.json" && restored="$restored auth.json"
    [ -f "$tmp_dir/aniworld.db" ] && cp "$tmp_dir/aniworld.db" "$DATA_DIR/aniworld.db" && restored="$restored aniworld.db"
    [ -f "$tmp_dir/metadata.db" ] && cp "$tmp_dir/metadata.db" "$DATA_DIR/metadata.db" && restored="$restored metadata.db"

    rm -rf "$tmp_dir"
    set_permissions

    echo ""
    echo -e "${GREEN}✅ Restore erfolgreich!${NC}"
    echo "  Wiederhergestellt:${BOLD}$restored${NC}"
    echo "  Pre-Restore Backup: $pre_restore_dir"
    echo ""

    read -p "Services neustarten? (j/n): " do_restart
    if [ "$do_restart" = "j" ] || [ "$do_restart" = "J" ]; then
        restart_services
    fi
    echo ""
    read -p "Enter für Menü..."
}

# ── Self-Update ────────────────────────────────────────────────────

self_update() {
    # Nicht updaten wenn --no-self-update übergeben wurde
    if [ "${SKIP_SELF_UPDATE:-}" = "1" ]; then
        return
    fi

    # Braucht curl
    if ! command -v curl &>/dev/null; then
        return
    fi

    echo -ne "${YELLOW}Prüfe auf Installer-Update...${NC} "

    # Aktuelle install.sh von GitHub holen und Hash vergleichen
    local tmp_installer
    tmp_installer=$(mktemp)
    if ! curl -sfL "$GITHUB_RAW/install.sh" -o "$tmp_installer" 2>/dev/null; then
        echo -e "${MUTED}(offline, übersprungen)${NC}"
        rm -f "$tmp_installer"
        return
    fi

    # Hash vergleichen
    local local_hash remote_hash
    local_hash=$(md5sum "$0" 2>/dev/null | cut -d' ' -f1)
    remote_hash=$(md5sum "$tmp_installer" 2>/dev/null | cut -d' ' -f1)

    if [ "$local_hash" = "$remote_hash" ]; then
        echo -e "${GREEN}✅ Aktuell${NC}"
        rm -f "$tmp_installer"
        return
    fi

    echo -e "${CYAN}Neue Version gefunden!${NC}"
    echo -e "${YELLOW}Installer wird aktualisiert und neu gestartet...${NC}"

    # Neue Version überschreiben
    cp "$tmp_installer" "$0"
    chmod +x "$0"
    rm -f "$tmp_installer"

    echo ""

    # Neu starten mit gleichen Argumenten, aber Self-Update überspringen
    SKIP_SELF_UPDATE=1 exec "$0" "$@"
}

# ── Main ───────────────────────────────────────────────────────────

check_root
check_emby
load_config
self_update "$@"

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
        6) reset_dashboard_password ;;
        7) create_backup ;;
        8) restore_backup ;;
        9) install_warp; read -p "Enter für Menü..." ;;
        10) uninstall ;;
        11) show_guide ;;
        0) echo "Bye! 👋"; exit 0 ;;
        *) echo -e "${RED}Ungültige Auswahl${NC}"; sleep 1 ;;
    esac
done
