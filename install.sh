#!/usr/bin/env bash
# =====================================================================
# install.sh - one-shot setup for the XAUUSD 1m scalper on Linux.
#
# What this does:
#   1. Installs system packages (python3, wine, xvfb, etc.)
#   2. Creates a Linux-side Python venv for the engine + dashboard
#   3. Creates a Wine prefix, installs the MetaTrader5 terminal in it,
#      and installs a Windows Python inside that same prefix (the
#      MetaTrader5 pip package needs real Windows DLLs, so the bridge
#      that talks to MT5 has to run under Wine's python, not the
#      system one - see bridge/mt5_bridge_server.py for why).
#   4. Writes .env from .env.example if it doesn't exist yet, asking
#      for broker credentials interactively (never hardcoded, never
#      committed - .env is gitignored).
#
# Safe to re-run: every step checks whether it's already done first.
# =====================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
VENV_DIR="$PROJECT_ROOT/.venv"
MT5_INSTALLER_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
WIN_PYTHON_URL="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install][warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[install][error]\033[0m %s\n' "$*" >&2; }

require_cmd() { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------- 1. apt
log "Step 1/5: system packages"
if require_cmd apt-get; then
    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi
    export DEBIAN_FRONTEND=noninteractive
    $SUDO dpkg --add-architecture i386 2>/dev/null || true
    $SUDO apt-get update -y
    $SUDO apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        wine wine64 winbind \
        xvfb xdotool cabextract wget curl unzip ca-certificates
else
    warn "apt-get not found. This installer targets Debian/Ubuntu."
    warn "Install the equivalent of: python3, python3-venv, wine, xvfb, wget, curl, cabextract manually, then re-run."
fi

if ! require_cmd wine; then
    err "wine is not available after the install step. Cannot continue - the MT5 terminal requires it."
    exit 1
fi

# ------------------------------------------------------- 2. python venv
log "Step 2/5: Linux-side Python venv (engine + dashboard)"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate
log "Linux venv ready at $VENV_DIR"

# --------------------------------------------------- 3. wine + MT5 + py
log "Step 3/5: Wine prefix + MetaTrader5 terminal + Windows Python"
export WINEPREFIX="$WINEPREFIX_DIR"
export WINEARCH=win64
export WINEDEBUG=-all

XVFB_CMD=""
if require_cmd xvfb-run; then
    XVFB_CMD="xvfb-run -a"
else
    warn "xvfb-run not found; wine installers will try to use a real display if present."
fi

if [ ! -d "$WINEPREFIX_DIR" ]; then
    log "Creating Wine prefix (this can take a minute)..."
    $XVFB_CMD wineboot --init
fi

MT5_MARKER="$WINEPREFIX_DIR/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ ! -f "$MT5_MARKER" ]; then
    log "Downloading MetaTrader 5 terminal installer..."
    wget -q -O /tmp/mt5setup.exe "$MT5_INSTALLER_URL" || {
        err "Could not download the MT5 installer from $MT5_INSTALLER_URL"
        err "Check network access, or download mt5setup.exe manually and place it at /tmp/mt5setup.exe, then re-run."
        exit 1
    }
    log "Installing MetaTrader 5 under Wine (silent)..."
    $XVFB_CMD wine /tmp/mt5setup.exe /auto || warn "MT5 installer returned non-zero; verifying install path anyway."
    sleep 5
fi
if [ -f "$MT5_MARKER" ]; then
    log "MetaTrader 5 terminal installed."
else
    warn "Could not confirm terminal64.exe at the expected path. Bridge start may fail - check $WINEPREFIX_DIR/drive_c manually."
fi

WIN_PYTHON="$WINEPREFIX_DIR/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
if [ ! -f "$WIN_PYTHON" ]; then
    log "Downloading Windows Python (needed inside Wine for the MetaTrader5 pip package)..."
    wget -q -O /tmp/winpython.exe "$WIN_PYTHON_URL" || {
        err "Could not download Windows Python installer from $WIN_PYTHON_URL"
        exit 1
    }
    log "Installing Windows Python under Wine (silent)..."
    $XVFB_CMD wine /tmp/winpython.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
    sleep 5
fi

if [ -f "$WIN_PYTHON" ]; then
    log "Installing MetaTrader5 + Flask into the Wine Python..."
    $XVFB_CMD wine "$WIN_PYTHON" -m pip install --upgrade pip -q
    $XVFB_CMD wine "$WIN_PYTHON" -m pip install -r bridge/requirements-bridge.txt -q
    log "Wine-side bridge environment ready."
else
    err "Windows Python not found at expected path after install: $WIN_PYTHON"
    err "The bridge (bridge/mt5_bridge_server.py) will not be able to run without it."
    err "You can retry this step alone by re-running install.sh."
fi

# --------------------------------------------------------- 4. .env file
log "Step 4/5: local configuration (.env)"
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    log "Created .env from .env.example."
    if [ -t 0 ]; then
        read -r -p "MT5 login (blank to fill in later): " input_login || true
        read -r -s -p "MT5 password (blank to fill in later): " input_pass || true
        echo
        read -r -p "MT5 server [FBS-Demo]: " input_server || true
        input_server="${input_server:-FBS-Demo}"
        if [ -n "${input_login:-}" ]; then
            sed -i "s|^MT5_LOGIN=.*|MT5_LOGIN=${input_login}|" "$PROJECT_ROOT/.env"
        fi
        if [ -n "${input_pass:-}" ]; then
            sed -i "s|^MT5_PASSWORD=.*|MT5_PASSWORD=${input_pass}|" "$PROJECT_ROOT/.env"
        fi
        sed -i "s|^MT5_SERVER=.*|MT5_SERVER=${input_server}|" "$PROJECT_ROOT/.env"
        log "Credentials saved to .env (this file is gitignored - it will never be committed or pushed)."
    else
        warn "Non-interactive shell: edit .env manually before running run.sh."
    fi
else
    log ".env already exists, leaving it as is."
fi

# Generate a bridge auth token if one isn't set yet - covers both a fresh
# .env (created above, BRIDGE_AUTH_TOKEN still blank from .env.example)
# and an existing .env from before this feature existed.
if ! grep -qE '^BRIDGE_AUTH_TOKEN=.+' "$PROJECT_ROOT/.env" 2>/dev/null; then
    token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    if grep -qE '^BRIDGE_AUTH_TOKEN=' "$PROJECT_ROOT/.env" 2>/dev/null; then
        sed -i "s|^BRIDGE_AUTH_TOKEN=.*|BRIDGE_AUTH_TOKEN=${token}|" "$PROJECT_ROOT/.env"
    else
        echo "BRIDGE_AUTH_TOKEN=${token}" >> "$PROJECT_ROOT/.env"
    fi
    log "Generated a bridge auth token (the bridge API will reject requests without it)."
fi

chmod 600 "$PROJECT_ROOT/.env" 2>/dev/null || true

# ------------------------------------------------------------ 5. done
log "Step 5/5: sanity check"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python3 -c "import pandas, numpy, flask, requests, dotenv, webview" && log "Linux dependencies import cleanly."
deactivate

cat <<'EOF'

=====================================================================
 Instalacion completa.

 Antes de operar en real (DRY_RUN=false en .env):
   1. Corre un backtest:  .venv/bin/python scripts/run_backtest.py
   2. Corre en modo simulado con precios reales del broker (DRY_RUN=true,
      valor por defecto) durante un tiempo y revisa el dashboard.
   3. Solo entonces, si los numeros te convencen, cambia DRY_RUN=false.

 Para arrancar todo:
   ./run.sh

 Para ver el dashboard:
   .venv/bin/python dashboard.py
=====================================================================
EOF
