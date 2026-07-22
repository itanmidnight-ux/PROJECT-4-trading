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
# Safe to re-run: every step checks whether it's already done first.
#
# Usage: ./install.sh [--no-wine]
# =====================================================================
set -euo pipefail

NO_WINE=0
for arg in "$@"; do
    case "$arg" in
        --no-wine) NO_WINE=1 ;;
        -h|--help)
            echo "Uso: ./install.sh [--no-wine]"
            echo "  --no-wine        (Kali/Ubuntu) No instalar Wine ni el terminal MT5 - para"
            echo "                   usar el bot con BROKER_KIND=oanda (conexion REST directa,"
            echo "                   sin MetaTrader en ninguna maquina). Ahorra varios GB."
            exit 0
            ;;
        *)
            echo "[install][error] Argumento desconocido: $arg (ver --help)" >&2
            exit 1
            ;;
    esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
MT5_INSTALLER_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
WIN_PYTHON_URL="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install][warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[install][error]\033[0m %s\n' "$*" >&2; }
progress() {
    local label="$1" pct="$2" width=28 filled empty
    filled=$((pct * width / 100)); empty=$((width - filled))
    printf '\r\033[1;36m[install]\033[0m %-24s [' "$label"
    printf '%*s' "$filled" '' | tr ' ' '#'
    printf '%*s' "$empty" '' | tr ' ' '.'
    printf '] %3d%%' "$pct"
    [ "$pct" -ge 100 ] && printf '\n'
}

require_cmd() { command -v "$1" >/dev/null 2>&1; }

require_installer_tools() {
    local missing=() cmd
    # python3 is deliberately not in this preflight: the Debian-like
    # branch installs it as its first package step on a clean machine.
    for cmd in bash sed grep awk; do
        require_cmd "$cmd" || missing+=("$cmd")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        err "Herramientas imprescindibles ausentes: ${missing[*]}"
        err "Debian/Kali/Ubuntu: sudo apt-get install bash coreutils sed grep gawk"
        return 1
    fi
}

# Wraps `pip install` with visible progress and a bounded timeout instead
# of pip's default quiet (-q), unbounded behavior - a stalled network or a
# package with no precompiled wheel for this platform (pip falling back
# to compiling it from source, common on ARM/Android/anything off the
# manylinux beaten path) used to look EXACTLY like the installer being
# frozen, with no way to tell "still working" from "actually stuck". Used
# by every platform branch below - a stalled download or a slow compile
# can happen on any system.
# $1: max seconds to allow. $@ (rest): passed straight through to
# `pip install`, e.g. `run_pip_install 1200 -r requirements.txt`.
run_pip_install() {
    local max_seconds="$1"
    shift
    if ! timeout "$max_seconds" pip install --progress-bar on "$@"; then
        err "pip install se paso de $((max_seconds / 60)) minutos, o fallo."
        err "Si fue un paquete compilando desde el codigo fuente (sin wheels"
        err "precompilados para esta arquitectura), instala las herramientas de compilacion primero"
        err "(ej. build-essential/clang, make, pkg-config) y volve a correr ./install.sh."
        return 1
    fi
}

# Same idea for a network download that could otherwise hang forever on a
# stalled connection instead of failing fast with a clear message.
# $1: URL, $2: output path.
download_with_timeout() {
    wget --timeout=30 --tries=3 -q -O "$2" "$1"
}

# Creates (or repairs) the venv, activates it, and PROVES the activation
# actually took - three real failure modes this covers that a bare
# `python3 -m venv && source activate` does not:
#   1. A partially-created venv from an interrupted earlier run (Ctrl-C,
#      disk full): the directory exists so naive `[ ! -d ]` skips creation,
#      then activate/pip are missing or broken and everything after fails
#      cryptically. Detected (bin/activate or bin/pip missing) -> recreated.
#   2. Activation that silently doesn't stick: if `pip` still resolves
#      OUTSIDE $VENV_DIR after sourcing activate, every dependency would
#      install into the system python instead of the venv - run.sh would
#      later report "faltan dependencias" with no obvious cause. Verified
#      with `command -v pip` and failed loudly here instead.
#   3. pip's self-upgrade stalling forever on a bad connection: bounded.
# $@: extra flags for `python3 -m venv` (e.g. --system-site-packages).
# Leaves the venv ACTIVATED on success; returns 1 (with errors printed) on
# any failure. Callers keep their own `deactivate`s.
ensure_venv() {
    if ! require_cmd timeout; then
        err "Falta la herramienta 'timeout' (coreutils); no puedo acotar instalaciones colgadas."
        return 1
    fi
    if [ -d "$VENV_DIR" ] && { [ ! -f "$VENV_DIR/bin/activate" ] || [ ! -x "$VENV_DIR/bin/pip" ]; }; then
        warn "El venv en $VENV_DIR esta incompleto/roto (probablemente una corrida anterior interrumpida) - recreandolo."
        rm -rf "$VENV_DIR"
    fi
    if [ ! -d "$VENV_DIR" ]; then
        if ! python3 -m venv "$@" "$VENV_DIR"; then
            err "No se pudo crear el venv en $VENV_DIR - revisa que el modulo 'venv' de Python este instalado"
            err "(en Debian/Ubuntu/Kali: apt install python3-venv) y que haya espacio en disco."
            return 1
        fi
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    case "$(command -v pip 2>/dev/null)" in
        "$VENV_DIR"/*) ;;  # pip is the venv's own - activation really took
        *)
            err "La activacion del venv no funciono: 'pip' resuelve a '$(command -v pip 2>/dev/null || echo ninguno)'"
            err "en vez de a $VENV_DIR/bin/pip. Instalar seguiria en el Python del sistema - abortando"
            err "en vez de instalar las dependencias en el lugar equivocado. Proba borrar $VENV_DIR y reintentar."
            return 1
            ;;
    esac
    timeout 300 pip install --upgrade pip -q \
        || warn "No se pudo actualizar pip (sin conexion o tardo >5min) - siguiendo con la version que ya hay."
    return 0
}

# ------------------------------------------------------- platform detection
# Told apart via /etc/os-release's ID (kali, ubuntu, ...) or ID_LIKE
# (debian) - falling back to "has apt-get" for any Debian derivative
# os-release didn't name explicitly, then "unknown" for anything else
# (Fedora, Arch, ...) this script doesn't have a tested path for.
detect_platform() {
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

# Same detection run.sh uses (duplicated on purpose - each script stays
# standalone): finds an EXISTING MetaTrader 5 install to reuse instead of
# downloading a second copy into the project's own prefix. Echoes
# "<prefix>\t<terminal64.exe>". The bridge must live in the SAME Wine
# prefix as the terminal (intra-prefix IPC), so reusing the user's real
# MT5 (e.g. one launched with the `metatrader` command) is not just a
# disk-space nicety - it's what makes the bot talk to THAT terminal.
detect_mt5_install() {
    local p t
    p="$(grep -E '^MT5_WINEPREFIX=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    t="$(grep -E '^MT5_TERMINAL_PATH=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    p="${p/#\~/$HOME}"; t="${t/#\~/$HOME}"
    if [ -n "$p" ] && [ -n "$t" ] && [ -f "$t" ]; then
        printf '%s\t%s\n' "$p" "$t"; return 0
    fi
    if [ -n "$p" ] && [ -f "$p/drive_c/Program Files/MetaTrader 5/terminal64.exe" ]; then
        printf '%s\t%s\n' "$p" "$p/drive_c/Program Files/MetaTrader 5/terminal64.exe"; return 0
    fi
    local candidates=("$WINEPREFIX_DIR")
    if command -v metatrader >/dev/null 2>&1; then
        local launcher lp
        launcher="$(command -v metatrader)"
        lp="$(grep -oE 'WINEPREFIX="?[^" ]+' "$launcher" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"
        [ -n "$lp" ] && candidates+=("${lp/#\~/$HOME}")
    fi
    candidates+=("$HOME/.mt5" "$HOME/.wine")
    for p in "${candidates[@]}"; do
        t="$p/drive_c/Program Files/MetaTrader 5/terminal64.exe"
        if [ -f "$t" ]; then
            printf '%s\t%s\n' "$p" "$t"; return 0
        fi
    done
    return 1
}

PLATFORM="$(detect_platform)"
log "Plataforma detectada: $PLATFORM"
progress "preflight" 10

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
    if [ "$NO_WINE" -eq 0 ]; then
        $SUDO dpkg --add-architecture i386 2>/dev/null || true
    fi
    # apt can hang indefinitely waiting on another apt/dpkg process holding
    # the lock (e.g. unattended-upgrades) - bounded here too rather than
    # blocking forever with no indication why.
    # shellcheck disable=SC2086  # $SUDO debe desaparecer (no ser un arg vacio "") cuando se corre como root - quoted, timeout intenta ejecutar '' y falla siempre
    if ! timeout 1200 $SUDO apt-get update -y; then
        err "apt-get update fallo o se paso de 20 minutos - revisa la conexion, o si otro proceso (unattended-upgrades) tiene el lock de apt/dpkg."
        exit 1
    fi
    local apt_packages="python3 python3-venv python3-pip wget curl unzip ca-certificates"
    if [ "$NO_WINE" -eq 0 ]; then
        apt_packages="$apt_packages wine wine64 winbind xvfb xdotool cabextract"
    else
        log "--no-wine: sin Wine/MT5 - para BROKER_KIND=oanda (REST directo). Configura OANDA_* en .env despues."
    fi
    # shellcheck disable=SC2086  # $apt_packages es una lista de paquetes a proposito
    if ! timeout 1800 $SUDO apt-get install -y --no-install-recommends $apt_packages; then
        err "apt-get install fallo o se paso de 30 minutos."
        exit 1
    fi

    if [ "$NO_WINE" -eq 0 ] && ! require_cmd wine; then
        err "wine no esta disponible despues de instalar. No puedo continuar - el terminal MT5 lo necesita."
        err "(Si vas a usar OANDA en vez de un broker MT5, corre ./install.sh --no-wine.)"
        exit 1
    fi

    log "Paso 2/5: venv de Python (Linux) - motor + dashboard"
    # --system-site-packages: sin esto el venv queda aislado de python3-gi
    # (recien instalado por apt arriba) y el dashboard nativo no puede
    # importar 'gi' desde adentro del venv aunque este instalado en el
    # sistema - confirmado en vivo (ImportError: No module named 'gi').
    # PyGObject no es pip-installable de forma confiable (necesita headers
    # de GTK/glib para compilar), asi que heredar del sistema es el camino
    # real, no un pip install mas. Un venv de una corrida anterior de
    # install.sh (antes de este fix) no tiene esa flag y nunca la va a
    # tener sin recrearlo - detectado y recreado aca.
    if [ -d "$VENV_DIR" ] && ! grep -q 'include-system-site-packages = true' "$VENV_DIR/pyvenv.cfg" 2>/dev/null; then
        warn "El venv existente no tiene --system-site-packages - recreandolo para que el dashboard nativo vea python3-gi del sistema."
        rm -rf "$VENV_DIR"
    fi
    ensure_venv --system-site-packages || exit 1
    if ! run_pip_install 1200 -r requirements.txt; then
        deactivate
        exit 1
    fi
    deactivate
    log "venv de Linux listo en $VENV_DIR"

    log "Paso 3/5: prefijo de Wine + terminal MetaTrader5 + Python de Windows"
    if [ "$NO_WINE" -eq 1 ]; then
        log "  Pasos 3 y 4 omitidos (--no-wine): este equipo se conectara por BROKER_KIND=oanda"
        log "  (REST directo, sin MetaTrader en ninguna maquina). Completa OANDA_API_TOKEN y"
        log "  OANDA_ACCOUNT_ID en .env - ver .env.example."
    else
    # (bloque Wine/MT5 sin re-indentar a proposito - bash no lo necesita y
    # mantiene el diff chico; el `else` de arriba y el `fi` antes del Paso
    # 5/5 son el unico cambio de estructura)

    # Antes de crear un prefijo propio y bajar el terminal: si esta maquina
    # YA tiene un MT5 instalado (ej. el lanzador `metatrader` del instalador
    # oficial de MetaQuotes para Linux), reutilizarlo. El bridge tiene que
    # correr en el MISMO prefijo Wine que el terminal (IPC intra-prefijo),
    # asi que usar el MT5 real del usuario - donde su cuenta ya esta
    # logueada - es lo correcto, no solo un ahorro de varios GB.
    EXISTING_MT5=""
    if EXISTING_MT5="$(detect_mt5_install)" && [ "$(printf '%s' "$EXISTING_MT5" | cut -f1)" != "$WINEPREFIX_DIR" ]; then
        WINEPREFIX_DIR="$(printf '%s' "$EXISTING_MT5" | cut -f1)"
        log "MetaTrader 5 ya esta instalado en esta maquina: $(printf '%s' "$EXISTING_MT5" | cut -f2)"
        log "Reutilizando ese prefijo Wine ($WINEPREFIX_DIR) en vez de instalar un segundo MT5 -"
        log "el Python de Windows y el paquete MetaTrader5 se instalan AHI para poder hablarle."
    fi
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
        if ! download_with_timeout "$MT5_INSTALLER_URL" /tmp/mt5setup.exe; then
            err "No se pudo descargar el instalador de MT5 desde $MT5_INSTALLER_URL"
            err "Revisa el acceso a internet, o descarga mt5setup.exe a mano a /tmp/mt5setup.exe y corre de nuevo."
            exit 1
        fi
        log "Instalando MetaTrader 5 bajo Wine (silencioso)..."
        # timeout como red de seguridad: /auto deberia ser silencioso, pero
        # si igual queda esperando un dialogo que nunca va a aparecer (modo
        # headless), esto corta en vez de colgarse para siempre.
        # shellcheck disable=SC2086  # $XVFB_CMD se separa a proposito en "xvfb-run -a" (o queda vacio)
        timeout 300 $XVFB_CMD wine /tmp/mt5setup.exe /auto || warn "El instalador de MT5 devolvio codigo distinto de cero o se paso de 5 minutos; verificando la ruta de instalacion de todos modos."
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
        if ! download_with_timeout "$WIN_PYTHON_URL" /tmp/winpython.exe; then
            err "No se pudo descargar el instalador de Python de Windows desde $WIN_PYTHON_URL"
            exit 1
        fi
        log "Instalando Python de Windows bajo Wine (silencioso)..."
        # shellcheck disable=SC2086  # $XVFB_CMD se separa a proposito en "xvfb-run -a" (o queda vacio)
        timeout 300 $XVFB_CMD wine /tmp/winpython.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 \
            || warn "El instalador de Python de Windows devolvio codigo distinto de cero o se paso de 5 minutos; verificando de todos modos."
        sleep 5
    fi

    if [ -f "$WIN_PYTHON" ]; then
        log "Paso 4/5: instalando MetaTrader5 + Flask en el Python de Wine..."
        # shellcheck disable=SC2086  # $XVFB_CMD se separa a proposito en "xvfb-run -a" (o queda vacio)
        timeout 600 $XVFB_CMD wine "$WIN_PYTHON" -m pip install --upgrade pip -q
        # shellcheck disable=SC2086
        if ! timeout 1800 $XVFB_CMD wine "$WIN_PYTHON" -m pip install -r bridge/requirements-bridge.txt -q; then
            err "pip (dentro de Wine) fallo o se paso de 30 minutos instalando bridge/requirements-bridge.txt."
            err "Podes reintentar solo este paso corriendo ./install.sh de nuevo."
            exit 1
        fi
        # Do not call the environment ready until the exact import the bridge
        # needs succeeds. This catches a wrong Wine prefix, a 32/64-bit
        # mismatch, or a pip install that silently went to another Python.
        if ! timeout 120 $XVFB_CMD wine "$WIN_PYTHON" -c "import MetaTrader5" >/dev/null 2>&1; then
            err "MetaTrader5 no se puede importar en el Python de Windows del mismo prefijo."
            err "Revisa que terminal64.exe y Python estén en el mismo WINEPREFIX y corre ./install.sh de nuevo."
            exit 1
        fi
        log "Entorno del bridge (lado Wine) verificado: MetaTrader5 importable."
    else
        err "No se encontro el Python de Windows en la ruta esperada: $WIN_PYTHON"
        err "El bridge (bridge/mt5_bridge_server.py) no va a poder correr sin el."
        err "Podes reintentar este paso solo, corriendo install.sh de nuevo."
        exit 1
    fi
    fi  # NO_WINE

    log "Paso 5/5: configuracion (.env)"
    setup_env_file
    if [ "$NO_WINE" -eq 1 ]; then
        # Sin Wine no hay camino mt5_bridge posible en esta maquina - dejar
        # .env apuntando al broker REST para que el motor Y la
        # re-verificacion final (run.sh doctor, que gatea sus chequeos de
        # Wine por BROKER_KIND) sean coherentes con lo que se instalo.
        if grep -qE '^BROKER_KIND=' "$PROJECT_ROOT/.env" 2>/dev/null; then
            sed -i 's|^BROKER_KIND=.*|BROKER_KIND=oanda|' "$PROJECT_ROOT/.env"
        else
            echo "BROKER_KIND=oanda" >> "$PROJECT_ROOT/.env"
        fi
        log "BROKER_KIND=oanda dejado en .env (instalacion --no-wine)."
    fi

    cat <<'EOF'

=====================================================================
 Instalacion completa.

 Antes de arrancar el motor real:
   1. Ejecuta la pestaña Backtesting MT5 (solo lectura).
   2. Confirma credenciales, símbolo, margen y límites de riesgo en Settings.
   3. Inicia el motor desde el dashboard; DRY_RUN=false envía órdenes reales.

 Para arrancar el servidor (bridge MT5 + dashboard):
   ./run.sh --start
   luego abrí http://127.0.0.1:9000 e iniciá el motor desde ahí cuando
   estes listo - el motor NO arranca solo con --start, a proposito.

 Otros comandos utiles:
   ./run.sh --status    estado actual (bridge, dashboard, motor, cuenta)
   ./run.sh --stop       apaga todo, no queda nada corriendo
   ./run.sh doctor         diagnostico completo (que falta, que esta bien)
=====================================================================
EOF
}

# --------------------------------------------------------- fallback path
install_unknown() {
    warn "No se reconocio la plataforma (no tiene apt-get)."
    warn "Este instalador soporta Kali y Ubuntu directamente."
    warn "Otras distros: instala a mano el equivalente de python3, python3-venv,"
    warn "wine, xvfb, wget, curl, cabextract, y volve a correr este script -"
    warn "va a seguir con el venv de Python y la configuracion de .env igual."

    log "venv de Python (Linux) - motor + dashboard"
    ensure_venv || exit 1
    if ! run_pip_install 1800 -r requirements.txt; then
        deactivate
        exit 1
    fi
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
            warn "Shell no interactiva: edita .env a mano antes de correr ./run.sh --start."
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

    # Add keys introduced by newer releases without overwriting credentials
    # or any operator-selected value in an existing .env.
    ensure_env_key() {
        local key="$1" value="$2"
        if ! grep -qE "^${key}=" "$PROJECT_ROOT/.env" 2>/dev/null; then
            printf '%s=%s\n' "$key" "$value" >> "$PROJECT_ROOT/.env"
            log "Agregada configuracion nueva: ${key}"
        fi
    }
    ensure_env_key AI_BRAIN_ENABLED false
    ensure_env_key OPENROUTER_MODEL openrouter/free
    ensure_env_key AI_BRAIN_TIMEOUT_MS 6000
    ensure_env_key AI_BRAIN_MAX_CALLS_PER_DAY 40
    ensure_env_key CANDLE_HISTORY_COUNT 600
    ensure_env_key AI_SUPERVISOR_ENABLED false
    ensure_env_key OPENROUTER_SUPERVISOR_MODEL openrouter/free
    ensure_env_key AI_SUPERVISOR_TIMEOUT_MS 6000
    ensure_env_key AI_SUPERVISOR_MAX_CALLS_PER_DAY 24

    chmod 600 "$PROJECT_ROOT/.env" 2>/dev/null || true
}

require_installer_tools || exit 1

case "$PLATFORM" in
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
