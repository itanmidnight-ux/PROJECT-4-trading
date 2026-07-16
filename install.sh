#!/usr/bin/env bash
# =====================================================================
# install.sh - one-shot setup for the XAUUSD 1m scalper. Auto-detects the
# host and adjusts what it installs:
#
#   - Kali / Ubuntu / other Debian-like Linux (has apt-get, real root or
#     sudo): full stack - system packages, a Linux-side Python venv, a
#     Wine prefix with the real MetaTrader5 terminal + a Windows Python
#     inside it (the MetaTrader5 pip package needs real Windows DLLs, so
#     the bridge that talks to MT5 has to run under Wine's python, not
#     the system one - see bridge/mt5_bridge_server.py for why). This
#     machine can run everything, including a local bridge with a real
#     broker connection.
#
#   - Termux on Android (no root): there is no reliable way to run a real
#     Windows GUI application (the MT5 terminal) under Wine on Android -
#     experimental proot/box64 combinations exist but are not something
#     this script can honestly claim to set up for a real-money bot. So
#     on Termux this installs ONLY the pure-Python side (engine,
#     dashboard, backtest - none of which need Wine) and configures it as
#     a CLIENT that talks to a bridge running on a separate real Linux
#     machine over the network (MT5_BRIDGE_URL points at that machine,
#     not 127.0.0.1). Backtesting and the dashboard work fully locally on
#     the phone either way.
#
# Safe to re-run: every step checks whether it's already done first.
# =====================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
MT5_INSTALLER_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
WIN_PYTHON_URL="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install][warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[install][error]\033[0m %s\n' "$*" >&2; }

require_cmd() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------- platform detection
# Termux sets $PREFIX to its own userland root (no root access, no real
# /usr) - the standard, most reliable way to detect it. $TERMUX_VERSION is
# a newer, additional signal some Termux builds also set. Everything else
# is told apart via /etc/os-release's ID (kali, ubuntu, ...) or ID_LIKE
# (debian) - falling back to "has apt-get" for any Debian derivative
# os-release didn't name explicitly, then "unknown" for anything else
# (Fedora, Arch, ...) this script doesn't have a tested path for.
detect_platform() {
    if [ "${PREFIX:-}" = "/data/data/com.termux/files/usr" ] || [ -n "${TERMUX_VERSION:-}" ]; then
        echo "termux"; return
    fi
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}" in
            kali) echo "kali"; return ;;
            ubuntu) echo "ubuntu"; return ;;
        esac
        case "${ID_LIKE:-}" in
            *debian*) echo "debian-like"; return ;;
        esac
    fi
    if require_cmd apt-get; then echo "debian-like"; return; fi
    echo "unknown"
}

PLATFORM="$(detect_platform)"
log "Plataforma detectada: $PLATFORM"

# ------------------------------------------------------------- Termux path
install_termux() {
    log "Termux/Android sin root: instalando solo el lado Python puro"
    log "(motor, dashboard, backtest) - Wine + terminal MT5 real no tienen"
    log "un camino confiable en Android, asi que esta maquina va a hablarle"
    log "a un bridge corriendo en otra Linux real (MT5_BRIDGE_URL remoto),"
    log "no a levantar uno local."

    log "Paso 1/3: paquetes de Termux"
    pkg update -y
    pkg install -y python coreutils

    log "Paso 2/3: venv de Python + dependencias"
    # numpy y pandas NO tienen wheels precompilados para Termux/Android en
    # PyPI (libc bionic, ABI distinto de manylinux) - si pip tiene que
    # compilarlos desde el codigo fuente puede tardar mucho (30-90+
    # minutos en un telefono real) o fallar sin las herramientas de
    # compilacion adecuadas, y como esto antes corria en modo silencioso
    # (-q, sin progreso visible) se veia exactamente igual a que el
    # instalador se hubiera colgado. Termux SI tiene sus propios paquetes
    # binarios para estos (compilados una vez por el equipo de Termux, no
    # por vos) - los probamos primero via pkg para evitar la compilacion
    # por completo; el venv se crea con --system-site-packages para poder
    # verlos desde adentro.
    local got_numpy_via_pkg=0 got_pandas_via_pkg=0
    if pkg install -y python-numpy 2>/dev/null; then
        got_numpy_via_pkg=1
        log "numpy instalado via pkg (binario, sin compilar)."
    else
        warn "python-numpy no esta disponible via pkg en este Termux - pip va a intentar compilarlo (puede tardar bastante)."
    fi
    if pkg install -y python-pandas 2>/dev/null; then
        got_pandas_via_pkg=1
        log "pandas instalado via pkg (binario, sin compilar)."
    else
        warn "python-pandas no esta disponible via pkg en este Termux - pip va a intentar compilarlo (puede tardar bastante, a veces 30+ minutos)."
    fi

    # A venv from a previous run of an older install.sh (or one that got
    # interrupted before this fix) may exist without --system-site-packages -
    # it would never see the numpy/pandas pkg installed above, so pip would
    # go straight back to compiling them from source on every re-run.
    # Recreate it rather than leave that stuck state around.
    if [ -d "$VENV_DIR" ] && ! grep -q 'include-system-site-packages = true' "$VENV_DIR/pyvenv.cfg" 2>/dev/null; then
        warn "El venv existente no tiene --system-site-packages - recreandolo para que vea numpy/pandas de pkg."
        rm -rf "$VENV_DIR"
    fi
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv --system-site-packages "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q

    # pywebview (ventana nativa) necesita un backend GTK/Qt real que Termux
    # no tiene - se omite a proposito (dashboard.py ya hace el import de
    # forma perezosa, --web funciona igual). numpy/pandas se omiten SOLO
    # si pkg ya los proveyo arriba, para que pip no los recompile encima
    # del binario que ya esta instalado.
    local skip_pattern='^pywebview'
    [ "$got_numpy_via_pkg" -eq 1 ] && skip_pattern="${skip_pattern}|^numpy"
    [ "$got_pandas_via_pkg" -eq 1 ] && skip_pattern="${skip_pattern}|^pandas"

    log "Instalando el resto de las dependencias con pip (con progreso visible)..."
    log "Si numpy o pandas terminan compilando desde el codigo fuente esto puede tardar bastante - no se colgo, mostrando progreso real."
    if ! grep -vE "$skip_pattern" requirements.txt | timeout 2400 pip install -r /dev/stdin --progress-bar on; then
        deactivate
        err "La instalacion de dependencias de Python fallo, o paso los 40 minutos de limite."
        err "Si fue numpy/pandas compilando desde el codigo fuente, instala las herramientas de compilacion primero:"
        err "  pkg install -y clang make pkg-config"
        err "y volve a correr ./install.sh. Tambien podes revisar 'pkg search python-pandas' para confirmar si tu"
        err "repo de Termux lo tiene como paquete binario (evita la compilacion por completo)."
        return 1
    fi
    deactivate
    log "venv listo en $VENV_DIR (sin pywebview - usa dashboard.py --web en Termux)"

    log "Paso 3/3: configuracion (.env)"
    setup_env_file
    log "IMPORTANTE: en .env, configura MT5_BRIDGE_URL con la IP/host de la"
    log "maquina Linux (Kali/Ubuntu) real que SI corre el bridge (ej."
    log "http://192.168.1.50:5001), y BRIDGE_AUTH_TOKEN con el mismo token"
    log "que install.sh genero en esa otra maquina (copialo de su .env)."

    cat <<'EOF'

=====================================================================
 Instalacion completa (modo Termux / cliente remoto).

 Esta maquina puede: correr el dashboard, correr backtests, y correr
 el motor en modo --synthetic para probar. Para trading real necesita
 un bridge corriendo en una Linux de verdad (Kali/Ubuntu) en la misma
 red o alcanzable por red - configuralo en MT5_BRIDGE_URL (.env) antes
 de correr ./run.sh sin --synthetic.

 Para ver el dashboard (recomendado --web en Termux, no hay entorno
 grafico nativo):
   .venv/bin/python dashboard.py --web

 Para probar el motor sin broker:
   ./run.sh --synthetic

 Diagnostico completo (que falta, que esta bien):
   ./run.sh doctor
=====================================================================
EOF
}

# --------------------------------------------------------- Debian-like path
install_debian_like() {
    log "Paso 1/5: paquetes de sistema (apt)"
    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then
        if require_cmd sudo; then
            SUDO="sudo"
        else
            err "No sos root y no hay 'sudo' disponible - no puedo instalar paquetes de sistema."
            err "Instala manualmente como root: python3 python3-venv python3-pip wine wine64 winbind xvfb xdotool cabextract wget curl unzip ca-certificates"
            exit 1
        fi
    fi
    export DEBIAN_FRONTEND=noninteractive
    $SUDO dpkg --add-architecture i386 2>/dev/null || true
    $SUDO apt-get update -y
    $SUDO apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        wine wine64 winbind \
        xvfb xdotool cabextract wget curl unzip ca-certificates

    if ! require_cmd wine; then
        err "wine no esta disponible despues de instalar. No puedo continuar - el terminal MT5 lo necesita."
        exit 1
    fi

    log "Paso 2/5: venv de Python (Linux) - motor + dashboard"
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    deactivate
    log "venv de Linux listo en $VENV_DIR"

    log "Paso 3/5: prefijo de Wine + terminal MetaTrader5 + Python de Windows"
    export WINEPREFIX="$WINEPREFIX_DIR"
    export WINEARCH=win64
    export WINEDEBUG=-all

    XVFB_CMD=""
    if require_cmd xvfb-run; then
        XVFB_CMD="xvfb-run -a"
    else
        warn "xvfb-run no encontrado; los instaladores de wine van a intentar usar una pantalla real si hay una."
    fi

    if [ ! -d "$WINEPREFIX_DIR" ]; then
        log "Creando prefijo de Wine (puede tardar un minuto)..."
        $XVFB_CMD wineboot --init
    fi

    MT5_MARKER="$WINEPREFIX_DIR/drive_c/Program Files/MetaTrader 5/terminal64.exe"
    if [ ! -f "$MT5_MARKER" ]; then
        log "Descargando el instalador de MetaTrader 5..."
        wget -q -O /tmp/mt5setup.exe "$MT5_INSTALLER_URL" || {
            err "No se pudo descargar el instalador de MT5 desde $MT5_INSTALLER_URL"
            err "Revisa el acceso a internet, o descarga mt5setup.exe a mano a /tmp/mt5setup.exe y corre de nuevo."
            exit 1
        }
        log "Instalando MetaTrader 5 bajo Wine (silencioso)..."
        $XVFB_CMD wine /tmp/mt5setup.exe /auto || warn "El instalador de MT5 devolvio codigo distinto de cero; verificando la ruta de instalacion de todos modos."
        sleep 5
    fi
    if [ -f "$MT5_MARKER" ]; then
        log "Terminal MetaTrader 5 instalado."
    else
        warn "No se pudo confirmar terminal64.exe en la ruta esperada. El bridge puede fallar al arrancar - revisa $WINEPREFIX_DIR/drive_c a mano."
    fi

    WIN_PYTHON="$WINEPREFIX_DIR/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
    if [ ! -f "$WIN_PYTHON" ]; then
        log "Descargando Python de Windows (lo necesita el paquete MetaTrader5 dentro de Wine)..."
        wget -q -O /tmp/winpython.exe "$WIN_PYTHON_URL" || {
            err "No se pudo descargar el instalador de Python de Windows desde $WIN_PYTHON_URL"
            exit 1
        }
        log "Instalando Python de Windows bajo Wine (silencioso)..."
        $XVFB_CMD wine /tmp/winpython.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
        sleep 5
    fi

    if [ -f "$WIN_PYTHON" ]; then
        log "Paso 4/5: instalando MetaTrader5 + Flask en el Python de Wine..."
        $XVFB_CMD wine "$WIN_PYTHON" -m pip install --upgrade pip -q
        $XVFB_CMD wine "$WIN_PYTHON" -m pip install -r bridge/requirements-bridge.txt -q
        log "Entorno del bridge (lado Wine) listo."
    else
        err "No se encontro el Python de Windows en la ruta esperada: $WIN_PYTHON"
        err "El bridge (bridge/mt5_bridge_server.py) no va a poder correr sin el."
        err "Podes reintentar este paso solo, corriendo install.sh de nuevo."
    fi

    log "Paso 5/5: configuracion (.env)"
    setup_env_file

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
   .venv/bin/python dashboard.py            # pregunta ventana nativa o web
   .venv/bin/python dashboard.py --web      # directo como pagina web (puerto 9000)

 Diagnostico completo (que falta, que esta bien):
   ./run.sh doctor
=====================================================================
EOF
}

# --------------------------------------------------------- fallback path
install_unknown() {
    warn "No se reconocio la plataforma (ni Termux, ni un Linux con apt-get)."
    warn "Este instalador soporta Kali, Ubuntu y Termux directamente."
    warn "Otras distros: instala a mano el equivalente de python3, python3-venv,"
    warn "wine, xvfb, wget, curl, cabextract, y volve a correr este script -"
    warn "va a seguir con el venv de Python y la configuracion de .env igual."

    log "venv de Python (Linux) - motor + dashboard"
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    deactivate
    log "venv listo en $VENV_DIR"

    setup_env_file

    if ! require_cmd wine; then
        warn "wine no esta instalado - el bridge local (MT5 real) no va a funcionar hasta que lo instales vos mismo e inicialices $WINEPREFIX_DIR."
    fi
}

# ------------------------------------------------------------- .env setup
# Shared across every platform branch above - the credential prompt and
# BRIDGE_AUTH_TOKEN generation don't depend on what else got installed.
setup_env_file() {
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
        log "Creado .env desde .env.example."
        if [ -t 0 ]; then
            read -r -p "MT5 login (vacio para completar despues): " input_login || true
            read -r -s -p "MT5 password (vacio para completar despues): " input_pass || true
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
            log "Credenciales guardadas en .env (este archivo esta en .gitignore - nunca se commitea ni se sube)."
        else
            warn "Shell no interactiva: edita .env a mano antes de correr run.sh."
        fi
    else
        log ".env ya existe, se deja como esta."
    fi

    # Bridge auth token: covers both a fresh .env (blank from .env.example)
    # and an existing .env from before this feature existed.
    if ! grep -qE '^BRIDGE_AUTH_TOKEN=.+' "$PROJECT_ROOT/.env" 2>/dev/null; then
        token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        if grep -qE '^BRIDGE_AUTH_TOKEN=' "$PROJECT_ROOT/.env" 2>/dev/null; then
            sed -i "s|^BRIDGE_AUTH_TOKEN=.*|BRIDGE_AUTH_TOKEN=${token}|" "$PROJECT_ROOT/.env"
        else
            echo "BRIDGE_AUTH_TOKEN=${token}" >> "$PROJECT_ROOT/.env"
        fi
        log "Generado un token de autenticacion para el bridge (la API del bridge va a rechazar pedidos sin el)."
    fi

    chmod 600 "$PROJECT_ROOT/.env" 2>/dev/null || true
}

case "$PLATFORM" in
    termux) install_termux ;;
    kali|ubuntu|debian-like) install_debian_like ;;
    *) install_unknown ;;
esac

# ------------------------------------------------------ final re-verification
# Don't just trust that the steps above worked - run the same diagnostic a
# user would run by hand (./run.sh doctor) and fail loudly if it still finds
# something missing. This is the single source of truth for "is this
# machine set up correctly" - reused as-is by ./run.sh doctor - so install.sh
# never drifts out of sync with what the doctor command actually checks.
# A "FALTA" (missing) item is a real install failure worth a nonzero exit;
# "AVISO" items (e.g. MT5 credentials still blank, DRY_RUN warnings) are not
# install failures - doctor itself already only returns nonzero on FALTA.
log "Paso final: re-verificando la instalacion (./run.sh doctor)"
echo
if ./run.sh doctor; then
    echo
    log "Re-verificacion OK: la instalacion esta completa y lista para usar."
else
    echo
    err "La re-verificacion encontro cosas que faltan (ver [FALTA] arriba)."
    err "Revisa los mensajes, resuelve lo que falta (a mano o corriendo ./install.sh de nuevo)."
    exit 1
fi
