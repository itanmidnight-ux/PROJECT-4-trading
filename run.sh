#!/usr/bin/env bash
# =====================================================================
# run.sh - the only other entrypoint besides install.sh. Auto-detects the
# platform (Kali/Ubuntu/Termux/other) the same way install.sh does, and
# adjusts what it does: on a machine with a local Wine prefix (set up by
# install.sh's Kali/Ubuntu path) it starts a local MT5 bridge; without one
# (Termux, or any install that skipped Wine) it talks to whatever
# MT5_BRIDGE_URL points at instead of assuming a local bridge exists.
#
# Usage:
#   ./run.sh --start     start the server (bridge + dashboard) in the
#                         background - NOT the trading engine itself, see
#                         below for why
#   ./run.sh --stop      stop EVERYTHING (engine, dashboard, bridge, Xvfb)
#                         - nothing is left running
#   ./run.sh --status    nicely formatted report of what's up right now
#
#   ./run.sh emergency-stop [--clear]   manual kill switch (see below)
#   ./run.sh verify          compile + tests + synthetic smoke test + dashboard API check
#   ./run.sh doctor          read-only diagnostic of the current install
#
# Why the engine isn't started by --start: it's the thing that actually
# places orders, and it's meant to be turned on deliberately, from the
# dashboard (web or native), not automatically the moment a server starts
# (e.g. after a reboot with a systemd unit - see
# scripts/xauusd-scalper.service.template). `./run.sh --start` brings up
# everything ELSE (the bridge, the dashboard at
# http://127.0.0.1:9000) so the dashboard is there to start/stop the
# engine from (POST /api/engine/start|stop) - see core/engine_supervisor.py
# for the crash-resilient (restart-with-backoff) process that spawns and
# controls, mirroring the exact policy this file used to apply to the
# engine directly before that control moved to the dashboard.
#
# Every component here (bridge, dashboard, and - separately - the engine
# once started from the dashboard) is supervised: if one dies unexpectedly
# it's restarted automatically with exponential backoff, instead of taking
# the whole system down.
# =====================================================================
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT" || exit 1

VENV_DIR="$PROJECT_ROOT/.venv"
WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
LOG_DIR="$PROJECT_ROOT/data/logs"
RUN_DIR="$PROJECT_ROOT/data/run"
STOP_FLAG="$RUN_DIR/stop.flag"
DASHBOARD_HOST="127.0.0.1"
DASHBOARD_PORT="9000"

log()  { printf '\033[1;36m[run]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[run][error]\033[0m %s\n' "$*" >&2; }

# dashboard.py picks its own port (see _find_free_port there): if
# DASHBOARD_PORT (default 9000, what we ASK it to try first) is already
# taken - e.g. by a leftover process from a previous crashed/root-owned
# run, exactly what broke here before - it silently moves to the next free
# one and records the REAL port in data/run/dashboard.port. Every place
# below that displays or health-checks the dashboard reads that file fresh
# (not a cached variable) so it never points at a stale/wrong port; falls
# back to the requested default if the dashboard hasn't started yet.
dashboard_port() {
    local p
    p="$(cat "$RUN_DIR/dashboard.port" 2>/dev/null)"
    case "$p" in
        ''|*[!0-9]*) echo "$DASHBOARD_PORT" ;;
        *) echo "$p" ;;
    esac
}

# ------------------------------------------------------- platform detection
# Same detection install.sh uses (duplicated on purpose, not sourced from
# there - each script stays runnable on its own). See install.sh for the
# reasoning behind each check.
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
    if command -v apt-get >/dev/null 2>&1; then echo "debian-like"; return; fi
    echo "unknown"
}

# ------------------------------------------------- MT5 install detection
# Finds a usable MetaTrader 5 installation and echoes two TAB-separated
# unix paths: "<wine_prefix>\t<terminal64.exe>". Priority order:
#   1. Explicit overrides in .env (MT5_WINEPREFIX / MT5_TERMINAL_PATH) -
#      always win when set and valid.
#   2. The project's own prefix ($PROJECT_ROOT/.wine, what install.sh sets
#      up from scratch).
#   3. A SYSTEM MT5 install the user already has - e.g. one launched with
#      a `metatrader` command (MetaQuotes' own Linux installer and several
#      distro packages create such a launcher). The launcher script is
#      grepped for its WINEPREFIX; common home prefixes are checked too.
# Reusing an existing install matters beyond convenience: the MetaTrader5
# pip package talks to the terminal over IPC WITHIN one Wine prefix, so
# the bridge MUST run in the same prefix as the terminal that has the
# account logged in - pointing the bot at its own empty prefix while the
# user's real MT5 lives elsewhere would never connect to that session.
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
        # Launcher scripts habitually contain `WINEPREFIX=/path wine ...` or
        # `export WINEPREFIX=...` - pull the path out if it's a text script.
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

# Converts a unix path INSIDE a wine prefix's drive_c to the Windows-style
# path the Wine-side python needs (e.g. .../prefix/drive_c/Program Files/x
# -> C:\Program Files\x).
wine_path_to_windows() {
    local prefix="$1" unix_path="$2"
    local rel="${unix_path#"$prefix"/drive_c/}"
    printf 'C:\\%s' "${rel//\//\\}"
}

# Stops whatever PID is in a pidfile, from OUTSIDE the process that
# started it (used by --stop, which runs as a fresh invocation of this
# script, not inside the backgrounded --start process - that one uses its
# own local kill_pidfile/cleanup/trap, see cmd_start_foreground).
kill_pidfile_external() {
    local pidfile="$1" label="$2"
    [ -f "$pidfile" ] || return 0
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "Deteniendo $label (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "$label no respondio a tiempo, forzando con SIGKILL."
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    rm -f "$pidfile"
}

# =================================================== cmd: emergency-stop
# Manual kill switch: touches the file the engine checks every poll cycle
# (see core/engine.py, KILL_SWITCH_PATH in .env). The engine force-closes
# any open position at market on its next step and halts - it does NOT
# rely on the engine process being reachable via `run.sh --stop`/kill,
# only on the filesystem being writable, so this also works if something
# (Wine hang, bridge stuck) is preventing a clean process shutdown. When
# the engine halts this way, main.py tells core/engine_supervisor.py
# directly (SIGTERM to ENGINE_SUPERVISOR_PID) not to restart it - this
# used to instead touch data/run/stop.flag, which is this file's OWN
# stop flag for the bridge/dashboard supervise() loop below, not
# anything engine-specific, so that would have wrongly stopped the
# server too. run_stop_flag here is now purely defensive cleanup for a
# stale leftover from that old behavior.
cmd_emergency_stop() {
    local kill_switch_path flag run_stop_flag
    kill_switch_path="$(grep -E '^KILL_SWITCH_PATH=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    kill_switch_path="${kill_switch_path:-data/EMERGENCY_STOP}"
    flag="$PROJECT_ROOT/$kill_switch_path"
    run_stop_flag="$PROJECT_ROOT/data/run/stop.flag"

    if [ "${1:-}" = "--clear" ]; then
        rm -f "$flag" "$run_stop_flag"
        log "Interruptor de emergencia desactivado ($flag eliminado)."
        log "Iniciá el motor de nuevo desde el dashboard para reanudar el trading."
        return 0
    fi

    mkdir -p "$(dirname "$flag")"
    touch "$flag"
    log "ACTIVADO: $flag creado."
    log "El motor cerrara cualquier posicion abierta a mercado y se detendra en su proximo ciclo de sondeo."
    log "Para reanudar despues: ./run.sh emergency-stop --clear, despues iniciar el motor desde el dashboard."
}

# ============================================================ cmd: verify
# Repeatable "does everything still work" check. Run after any code
# change, before trusting a real (even demo) run. Does NOT need Wine/MT5/a
# broker connection - everything here runs against synthetic data or an
# in-memory/temp SQLite DB, so it works the same on Termux as on Kali/Ubuntu.
cmd_verify() {
    if [ ! -d ".venv" ]; then
        err "No se encontro .venv. Corre ./install.sh primero (o python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt)."
        return 1
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate

    if ! python3 -c "import pytest" 2>/dev/null; then
        log "Instalando dependencias de test (requirements-dev.txt)..."
        pip install -q -r requirements-dev.txt
    fi

    log "1/4  Compilando todos los modulos Python..."
    if ! python3 -m py_compile core/*.py main.py dashboard.py scripts/*.py; then
        deactivate; return 1
    fi
    if ! python3 -c "import ast; ast.parse(open('bridge/mt5_bridge_server.py').read())"; then
        deactivate; return 1
    fi
    log "     OK"

    log "2/4  Corriendo tests (pytest)..."
    if ! python3 -m pytest tests/ -q; then
        deactivate; return 1
    fi
    log "     OK"

    log "3/4  Prueba de humo: motor en modo --synthetic durante 12s (sin broker)..."
    local tmp_log
    tmp_log="$(mktemp)"
    ( timeout 12 python3 -u main.py --synthetic --log-level INFO >"$tmp_log" 2>&1 ) || true
    if grep -q "Engine started" "$tmp_log"; then
        log "     OK (el motor arranco y corrio el loop sin excepciones no controladas)"
    else
        err "El motor no llego a arrancar. Salida:"
        cat "$tmp_log" >&2
        rm -f "$tmp_log"
        deactivate; return 1
    fi
    if grep -qi "Traceback" "$tmp_log"; then
        err "Se encontro un traceback durante la prueba de humo:"
        cat "$tmp_log" >&2
        rm -f "$tmp_log"
        deactivate; return 1
    fi
    rm -f "$tmp_log"

    log "4/4  Validando que dashboard.py sirve su API sin errores..."
    if ! python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
import dashboard as dmod
client = dmod.app.test_client()
for path in ("/", "/app.js", "/style.css", "/api/status", "/api/summary",
             "/api/equity_curve", "/api/pnl_daily", "/api/pnl_monthly",
             "/api/trades", "/api/events", "/api/settings"):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
print("     OK (todas las rutas del dashboard responden 200)")
PYEOF
    then
        deactivate; return 1
    fi

    deactivate

    cat <<'EOF'

=====================================================================
 Todo verificado: compila, los tests pasan, el motor corre sin
 excepciones con datos sinteticos, y el dashboard sirve su API.

 Esto NO prueba que la estrategia sea rentable, ni que la conexion
 real a MT5/FBS funcione (eso necesita Wine + el bridge corriendo en
 una maquina Kali/Ubuntu, o un bridge remoto alcanzable desde aca).
 Para eso: ./install.sh, luego ./run.sh --start y abrí el dashboard.
=====================================================================
EOF
}

# ============================================================ cmd: doctor
# Read-only diagnostic. Never installs or changes anything itself - see
# install.sh for that. Platform-aware: on Termux (no local Wine, ever) it
# checks remote-bridge reachability instead of reporting Wine/MT5/Xvfb as
# "missing", which they're supposed to be on that platform.
cmd_doctor() {
    local platform pass=0 fail=0 warn_count=0
    platform="$(detect_platform)"

    ok()      { printf '  \033[1;32m[OK]\033[0m       %s\n' "$*"; pass=$((pass+1)); }
    bad()     { printf '  \033[1;31m[FALTA]\033[0m    %s\n' "$*"; fail=$((fail+1)); }
    warn_msg() { printf '  \033[1;33m[AVISO]\033[0m    %s\n' "$*"; warn_count=$((warn_count+1)); }
    section() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

    section "0. Plataforma"
    ok "detectada: $platform"

    section "1. Entorno Python"
    if [ -d ".venv" ]; then
        ok ".venv existe"
        if [ "$platform" = "termux" ]; then
            # Split on purpose: dashboard.py never imports pandas/numpy (only
            # core/strategy.py, core/backtest.py etc. do, reachable only
            # through the engine) - numpy/pandas are known to sometimes fail
            # to compile from source on Termux/ARM (a real toolchain
            # limitation, see install.sh's install_termux), so a missing
            # pandas there is a warning about the engine/backtest, not a
            # "the whole install is broken" failure the way a missing Flask
            # would be.
            if .venv/bin/python3 -c "import flask, requests, dotenv" 2>/dev/null; then
                ok "dependencias del dashboard (Flask/requests/python-dotenv) importan correctamente"
            else
                bad "faltan dependencias basicas del dashboard en .venv - corre ./install.sh"
            fi
            if .venv/bin/python3 -c "import pandas, numpy" 2>/dev/null; then
                ok "pandas/numpy importan correctamente (motor y backtests disponibles aca)"
            else
                warn_msg "pandas/numpy no estan instalados - el motor y los backtests NO van a funcionar en esta"
                warn_msg "maquina (el dashboard si). Normal en algunos telefonos/toolchains de Termux - ver los"
                warn_msg "avisos de ./install.sh, o usa esta maquina solo como cliente remoto del dashboard."
            fi
        elif .venv/bin/python3 -c "import pandas, numpy, flask, requests, dotenv, webview" 2>/dev/null; then
            ok "dependencias de requirements.txt importan correctamente"
        else
            bad "faltan dependencias en .venv - corre: .venv/bin/pip install -r requirements.txt"
        fi
    else
        bad "no existe .venv - corre ./install.sh"
    fi

    section "2. Configuracion (.env)"
    if [ -f ".env" ]; then
        ok ".env existe"
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
        if [ "${BROKER_KIND:-mt5_bridge}" = "oanda" ]; then
            if [ -n "${OANDA_API_TOKEN:-}" ] && [ -n "${OANDA_ACCOUNT_ID:-}" ]; then
                ok "BROKER_KIND=oanda con OANDA_API_TOKEN y OANDA_ACCOUNT_ID configurados"
            else
                warn_msg "BROKER_KIND=oanda pero faltan OANDA_API_TOKEN y/o OANDA_ACCOUNT_ID en .env - el motor no va a poder conectar hasta completarlos (cuenta practice gratis en oanda.com)."
            fi
        elif [ -n "${MT5_LOGIN:-}" ] && [ -n "${MT5_PASSWORD:-}" ]; then
            ok "MT5_LOGIN y MT5_PASSWORD estan configurados"
        else
            # Not a hard failure on purpose: an install is fully functional
            # without credentials yet (dashboard-only Termux client, or "fill
            # them in later") - they're completable any time from the
            # dashboard's Settings tab, so this must never block install.sh's
            # final re-verification the way a genuinely broken .venv/.env would.
            warn_msg "MT5_LOGIN o MT5_PASSWORD vacios en .env - editalo, corre ./install.sh, o completalo desde la pestaña Settings del dashboard"
        fi
        if [ "${DRY_RUN:-true}" = "true" ]; then
            ok "DRY_RUN=true (modo seguro: precios reales, sin ordenes reales)"
        else
            warn_msg "DRY_RUN=false: el motor mandara ORDENES REALES a la cuenta ${MT5_LOGIN:-?} (${MT5_SERVER:-?})"
        fi
        if [ -n "${BRIDGE_AUTH_TOKEN:-}" ]; then
            ok "BRIDGE_AUTH_TOKEN configurado (el bridge exige autenticacion)"
        else
            warn_msg "BRIDGE_AUTH_TOKEN vacio - el bridge correra sin autenticacion. Corre ./install.sh para generarlo."
        fi
        local perms
        perms=$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null || echo '?')
        if [ "$perms" != "600" ]; then
            warn_msg ".env tiene permisos $perms (se recomienda 600: chmod 600 .env)"
        fi
        local kill_switch="${KILL_SWITCH_PATH:-data/EMERGENCY_STOP}"
        if [ -f "$PROJECT_ROOT/$kill_switch" ]; then
            warn_msg "El interruptor de emergencia esta ACTIVO ($kill_switch existe) - el motor no abrira operaciones. Corre ./run.sh emergency-stop --clear para reanudar."
        else
            ok "Interruptor de emergencia inactivo ($kill_switch no existe)"
        fi
        local pause_flag="${PAUSE_FLAG_PATH:-data/PAUSED}"
        if [ -f "$PROJECT_ROOT/$pause_flag" ]; then
            warn_msg "El bot esta PAUSADO manualmente ($pause_flag existe) - no abrira operaciones nuevas hasta \"Reanudar entradas\" en el dashboard."
        else
            ok "Bot no pausado ($pause_flag no existe)"
        fi
        if [ -n "${DASHBOARD_AUTH_TOKEN:-}" ]; then
            ok "DASHBOARD_AUTH_TOKEN configurado (Settings, pausar/reanudar, e iniciar/detener el motor exigen autenticacion)"
        else
            warn_msg "DASHBOARD_AUTH_TOKEN vacio - sin importancia en uso local; configuralo si vas a usar --web --host 0.0.0.0."
        fi
    else
        bad "no existe .env - corre ./install.sh"
    fi

    if [ "${BROKER_KIND:-mt5_bridge}" = "oanda" ]; then
        section "3. Conexion directa OANDA (BROKER_KIND=oanda - sin MT5, sin Wine, sin bridge)"
        ok "este modo no necesita Wine/MT5/bridge en NINGUNA maquina - HTTPS directo"
        if [ -n "${OANDA_API_TOKEN:-}" ] && [ -n "${OANDA_ACCOUNT_ID:-}" ]; then
            local oanda_base="https://api-fxpractice.oanda.com"
            [ "${OANDA_ENV:-practice}" = "live" ] && oanda_base="https://api-fxtrade.oanda.com"
            if curl -fsS --max-time 10 -H "Authorization: Bearer ${OANDA_API_TOKEN}" \
                    "${oanda_base}/v3/accounts/${OANDA_ACCOUNT_ID}/summary" >/dev/null 2>&1; then
                ok "OANDA (${OANDA_ENV:-practice}) responde y acepta las credenciales"
            else
                warn_msg "OANDA no respondio o rechazo las credenciales - revisa OANDA_API_TOKEN/OANDA_ACCOUNT_ID/OANDA_ENV y la conexion a internet."
            fi
        fi
    elif [ "$platform" = "termux" ]; then
        section "3. Bridge remoto (Termux no corre Wine/MT5 local)"
        local bridge_url="${MT5_BRIDGE_URL:-http://127.0.0.1:5001}"
        case "$bridge_url" in
            *127.0.0.1*|*localhost*)
                warn_msg "MT5_BRIDGE_URL ($bridge_url) apunta a esta misma maquina, pero Termux no corre un bridge local - configuralo con la IP de una Kali/Ubuntu real que si lo tenga."
                ;;
            *)
                ok "MT5_BRIDGE_URL configurado como remoto: $bridge_url"
                case "$bridge_url" in
                    http://*)
                        warn_msg "el bridge remoto es http:// (sin cifrar): las credenciales MT5 viajan en texto plano por esa red. En una red local propia puede ser aceptable; si no, usa un tunel SSH o una VPN tipo Tailscale (ver README)."
                        ;;
                esac
                local bridge_latency
                if bridge_latency=$(curl -fsS -o /dev/null -w '%{time_total}' --max-time 10 "${bridge_url}/health" 2>/dev/null); then
                    # Latency matters here: the engine makes several bridge
                    # calls per poll cycle, each paying this round-trip.
                    if awk "BEGIN{exit !($bridge_latency > 1.0)}"; then
                        warn_msg "el bridge remoto responde pero LENTO (${bridge_latency}s por request) - el motor hace varias llamadas por ciclo; revisa la red/Wi-Fi entre esta maquina y el bridge."
                    else
                        ok "el bridge remoto responde (latencia ${bridge_latency}s)"
                    fi
                else
                    warn_msg "el bridge remoto no responde ahora mismo (normal si esa maquina no lo tiene corriendo)"
                fi
                ;;
        esac
    else
        section "3. Wine + MetaTrader 5"
        if command -v wine >/dev/null 2>&1; then
            ok "wine instalado ($(wine --version 2>/dev/null || echo 'version desconocida'))"
        else
            bad "wine no esta instalado - corre ./install.sh"
        fi

        local mt5_install doctor_prefix
        if mt5_install="$(detect_mt5_install)"; then
            doctor_prefix="$(printf '%s' "$mt5_install" | cut -f1)"
            if [ "$doctor_prefix" = "$WINEPREFIX_DIR" ]; then
                ok "terminal MetaTrader 5 instalado (prefijo del proyecto: $WINEPREFIX_DIR)"
            else
                ok "terminal MetaTrader 5 instalado (instalacion del sistema detectada: $(printf '%s' "$mt5_install" | cut -f2))"
            fi
        else
            doctor_prefix="$WINEPREFIX_DIR"
            bad "no se encontro ninguna instalacion de MetaTrader 5 (ni $WINEPREFIX_DIR, ni el comando 'metatrader', ni ~/.wine|~/.mt5) - corre ./install.sh, o fija MT5_WINEPREFIX/MT5_TERMINAL_PATH en .env si esta en otra ruta"
        fi

        local win_python
        win_python="$doctor_prefix/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
        if [ -f "$win_python" ]; then
            ok "python de Windows (para el bridge) instalado en el prefijo del MT5"
            export WINEPREFIX="$doctor_prefix"
            export WINEDEBUG=-all
            if wine "$win_python" -c "import MetaTrader5" >/dev/null 2>&1; then
                ok "paquete MetaTrader5 importable dentro de Wine"
            else
                bad "el paquete MetaTrader5 no importa en el python de Wine - corre ./install.sh de nuevo"
            fi
        else
            bad "no se encontro el python de Windows dentro de Wine - corre ./install.sh"
        fi

        section "4. Pantalla virtual (Xvfb)"
        if command -v Xvfb >/dev/null 2>&1; then
            ok "Xvfb instalado"
        else
            warn_msg "Xvfb no encontrado - run.sh lo necesita si no hay DISPLAY configurado"
        fi
    fi

    section "5. Procesos (bridge, dashboard, motor)"
    if curl -fsS "http://127.0.0.1:5001/health" >/dev/null 2>&1; then
        local health_json
        health_json=$(curl -fsS "http://127.0.0.1:5001/health" 2>/dev/null)
        if echo "$health_json" | grep -q '"connected": *true'; then
            ok "bridge corriendo (local) y con sesion MT5 activa"
            # With a live MT5 session, confirm the configured symbol actually
            # resolves on THIS account - FBS Micro/Cent accounts rename it
            # with a suffix (XAUUSDm/XAUUSDc); the bridge auto-resolves that,
            # and this surfaces the mapping (or a real failure) at diagnosis
            # time instead of at the first trade attempt.
            local sym_json resolved_sym
            sym_json=$(curl -fsS --max-time 10 -H "X-Bridge-Token: ${BRIDGE_AUTH_TOKEN:-}" \
                "http://127.0.0.1:5001/symbol/${SYMBOL:-XAUUSD}" 2>/dev/null) || sym_json=""
            if echo "$sym_json" | grep -q '"ok": *true'; then
                resolved_sym=$(echo "$sym_json" | grep -o '"symbol": *"[^"]*"' | cut -d'"' -f4)
                if [ -n "$resolved_sym" ] && [ "$resolved_sym" != "${SYMBOL:-XAUUSD}" ]; then
                    ok "simbolo ${SYMBOL:-XAUUSD} disponible en esta cuenta como '$resolved_sym' (sufijo del broker, resuelto automaticamente)"
                else
                    ok "simbolo ${SYMBOL:-XAUUSD} disponible en esta cuenta"
                fi
            elif [ -n "$sym_json" ]; then
                warn_msg "el simbolo ${SYMBOL:-XAUUSD} NO resuelve en esta cuenta (ni con variantes) - revisa el simbolo exacto en tu MT5/FBS y ajusta SYMBOL en .env"
            fi
        else
            warn_msg "bridge corriendo (local) pero SIN sesion MT5 activa (falta /login o credenciales invalidas)"
        fi
    else
        warn_msg "bridge no responde en 127.0.0.1:5001 (normal si ./run.sh --start no esta corriendo, o si el bridge es remoto)"
    fi

    if curl -fsS "http://${DASHBOARD_HOST}:$(dashboard_port)/api/status" >/dev/null 2>&1; then
        local status_json
        status_json=$(curl -fsS "http://${DASHBOARD_HOST}:$(dashboard_port)/api/status" 2>/dev/null)
        ok "dashboard corriendo en http://${DASHBOARD_HOST}:$(dashboard_port)"
        if echo "$status_json" | grep -q '"engine_running": *true'; then
            ok "motor de trading CORRIENDO (iniciado desde el dashboard)"
        else
            warn_msg "motor de trading detenido - iniciarlo desde el dashboard cuando estes listo"
        fi
    else
        warn_msg "dashboard no responde en http://${DASHBOARD_HOST}:$(dashboard_port) (normal si ./run.sh --start no esta corriendo)"
        if [ -f "$RUN_DIR/engine.pid" ] && kill -0 "$(cat "$RUN_DIR/engine.pid" 2>/dev/null)" 2>/dev/null; then
            warn_msg "hay un motor de trading corriendo (PID $(cat "$RUN_DIR/engine.pid")) pero el dashboard que lo controla no responde"
        fi
    fi

    if [ -f "$RUN_DIR/supervisor.pid" ] && kill -0 "$(cat "$RUN_DIR/supervisor.pid" 2>/dev/null)" 2>/dev/null; then
        ok "el servidor (./run.sh --start) esta corriendo (PID $(cat "$RUN_DIR/supervisor.pid"))"
    else
        warn_msg "el servidor no esta corriendo (./run.sh --start para arrancarlo)"
    fi

    section "6. Espacio en disco"
    local avail_kb avail_mb
    avail_kb=$(df -Pk "$PROJECT_ROOT" | awk 'NR==2 {print $4}')
    avail_mb=$(( avail_kb / 1024 ))
    local min_needed=500
    [ "$platform" != "termux" ] && min_needed=2000  # Wine + MT5 need several GB; pure-Python side needs much less
    if [ "$avail_mb" -lt 500 ]; then
        bad "solo quedan ${avail_mb}MB de disco libres"
    elif [ "$avail_mb" -lt "$min_needed" ]; then
        warn_msg "quedan ${avail_mb}MB de disco libres - puede quedar justo"
    else
        ok "espacio en disco suficiente (${avail_mb}MB libres)"
    fi

    section "Resumen"
    printf '  \033[1;32m%d OK\033[0m   \033[1;33m%d avisos\033[0m   \033[1;31m%d faltan\033[0m\n' "$pass" "$warn_count" "$fail"
    if [ "$fail" -gt 0 ]; then
        echo "  Corre ./install.sh para resolver lo que falta arriba."
        return 1
    fi
    return 0
}

# ============================================================= cmd: --stop
# Stops EVERYTHING: the trading engine (if the dashboard started one), the
# server supervisor (which in turn stops the bridge, the dashboard, and
# Xvfb via its own cleanup trap), and - belt and suspenders - any
# individual component pidfile left over from an unclean shutdown (e.g.
# the supervisor itself was killed with -9, so its trap never ran).
cmd_stop_all() {
    local anything=0

    if [ -f "$RUN_DIR/engine.pid" ]; then
        kill_pidfile_external "$RUN_DIR/engine.pid" "motor de trading (bot)"
        anything=1
    fi

    if [ -f "$RUN_DIR/supervisor.pid" ]; then
        kill_pidfile_external "$RUN_DIR/supervisor.pid" "supervisor del servidor"
        anything=1
    fi

    local pf
    for pf in bridge.pid dashboard.pid xvfb.pid; do
        if [ -f "$RUN_DIR/$pf" ]; then
            kill_pidfile_external "$RUN_DIR/$pf" "$pf"
            anything=1
        fi
    done

    rm -f "$STOP_FLAG"

    # Mirror of the wake lock taken in cmd_start: nothing is running
    # anymore, so stop keeping the phone awake (battery). Safe to call
    # even if no lock was ever taken.
    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock 2>/dev/null || true
        log "Wake lock de Termux liberado."
    fi

    if [ "$anything" -eq 0 ]; then
        log "No habia nada corriendo."
    else
        log "Todo detenido - no queda nada corriendo."
    fi
}

# =========================================================== cmd: --status
# A nicely formatted snapshot of what's actually running right now -
# pulls real account data from the dashboard's own /api/status when it's
# reachable (the same data the dashboard UI shows), so this is a real
# status view, not just "is the process alive".
cmd_status() {
    local platform
    platform="$(detect_platform)"

    local GREEN='\033[1;32m' RED='\033[1;31m' YELLOW='\033[1;33m' DIM='\033[2m' BOLD='\033[1m' RESET='\033[0m'
    row() { printf "  %-16s %b\n" "$1" "$2"; }

    printf "%b" "$BOLD"
    echo "========================================================"
    echo "  XAUUSD Scalper -- Estado"
    echo "========================================================"
    printf "%b\n" "$RESET"

    row "Plataforma" "$platform"

    # ---- kill switch: a filesystem check, independent of whether the
    # dashboard/bridge respond, so it shows up even when everything else
    # is down - the whole point of the kill switch is working without a
    # process being reachable. Surfaced here (not just in ./run.sh doctor)
    # because it directly explains an otherwise-confusing "engine says
    # running but never opens a trade" - doctor is a deep diagnostic
    # someone has to think to run, --status is the one people check first.
    local kill_switch_path=""
    [ -f ".env" ] && kill_switch_path="$(grep -E '^KILL_SWITCH_PATH=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
    kill_switch_path="${kill_switch_path:-data/EMERGENCY_STOP}"
    if [ -f "$PROJECT_ROOT/$kill_switch_path" ]; then
        row "Interruptor" "${RED}●${RESET} EMERGENCY STOP ACTIVO -- el motor no abrira operaciones. ./run.sh emergency-stop --clear para reanudar."
    fi
    echo

    # ---- bridge ----
    local bridge_url is_local_bridge="0"
    if [ -f ".env" ]; then
        bridge_url="$(grep -E '^MT5_BRIDGE_URL=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
    fi
    bridge_url="${bridge_url:-http://127.0.0.1:5001}"
    case "$bridge_url" in *127.0.0.1*|*localhost*) is_local_bridge="1" ;; esac

    if curl -fsS "${bridge_url}/health" >/dev/null 2>&1; then
        local health_json connected="false"
        health_json=$(curl -fsS "${bridge_url}/health" 2>/dev/null)
        echo "$health_json" | grep -q '"connected": *true' && connected="true"
        if [ "$connected" = "true" ]; then
            row "Bridge MT5" "${GREEN}●${RESET} activo $([ "$is_local_bridge" = "1" ] && echo "(local)" || echo "(remoto: $bridge_url)") -- sesion MT5: conectada"
        else
            row "Bridge MT5" "${YELLOW}●${RESET} activo $([ "$is_local_bridge" = "1" ] && echo "(local)" || echo "(remoto: $bridge_url)") -- sesion MT5: SIN conectar"
        fi
    else
        row "Bridge MT5" "${RED}●${RESET} detenido/inalcanzable ($bridge_url)"
    fi

    # ---- dashboard + engine + account (via the dashboard's own API) ----
    if curl -fsS "http://${DASHBOARD_HOST}:$(dashboard_port)/api/status" >/dev/null 2>&1; then
        local status_json
        status_json=$(curl -fsS "http://${DASHBOARD_HOST}:$(dashboard_port)/api/status" 2>/dev/null)
        row "Dashboard" "${GREEN}●${RESET} activo -- http://${DASHBOARD_HOST}:$(dashboard_port)"

        if echo "$status_json" | grep -q '"engine_running": *true'; then
            local mode paused
            mode=$(echo "$status_json" | grep -o '"mode": *"[^"]*"' | cut -d'"' -f4)
            paused="no"
            echo "$status_json" | grep -q '"paused": *true' && paused="SI (pausado)"
            row "Motor / Bot" "${GREEN}●${RESET} corriendo -- modo: ${mode:-?}  pausado: ${paused}"
        else
            row "Motor / Bot" "${DIM}●${RESET} detenido -- iniciarlo desde el dashboard cuando estes listo"
        fi

        echo
        local login server connected
        login=$(echo "$status_json" | grep -o '"login": *"[^"]*"' | cut -d'"' -f4)
        server=$(echo "$status_json" | grep -o '"server": *"[^"]*"' | cut -d'"' -f4)
        connected="no"
        echo "$status_json" | grep -q '"connected": *true' && connected="si"
        row "Cuenta" "${login:-sin configurar} @ ${server:-?}"
        row "Datos recientes" "$connected"
    else
        row "Dashboard" "${RED}●${RESET} detenido/inalcanzable -- corre ./run.sh --start"
        if [ -f "$RUN_DIR/engine.pid" ] && kill -0 "$(cat "$RUN_DIR/engine.pid" 2>/dev/null)" 2>/dev/null; then
            row "Motor / Bot" "${YELLOW}●${RESET} corriendo (PID $(cat "$RUN_DIR/engine.pid")) pero el dashboard que lo controla no responde"
        else
            row "Motor / Bot" "${DIM}●${RESET} detenido"
        fi
    fi

    printf "%b" "$BOLD"
    echo
    echo "========================================================"
    printf "%b" "$RESET"
    echo "  ./run.sh --stop        detener todo (motor + dashboard + bridge)"
    echo "  ./run.sh doctor        diagnostico completo"
    if curl -fsS "http://${DASHBOARD_HOST}:$(dashboard_port)/api/status" >/dev/null 2>&1; then
        echo "  Dashboard (control del bot): http://${DASHBOARD_HOST}:$(dashboard_port)"
    fi
    printf "%b" "$BOLD"
    echo "========================================================"
    printf "%b" "$RESET"
}

# ============================================================= cmd: --start
cmd_start() {
    if [ -f "$RUN_DIR/supervisor.pid" ] && kill -0 "$(cat "$RUN_DIR/supervisor.pid" 2>/dev/null)" 2>/dev/null; then
        err "Ya hay una instancia corriendo (PID $(cat "$RUN_DIR/supervisor.pid")). Usa ./run.sh --stop primero."
        return 1
    fi
    if [ ! -d "$VENV_DIR" ]; then
        err "No se encontro $VENV_DIR. Corre ./install.sh primero."
        return 1
    fi
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        err "No se encontro .env. Corre ./install.sh primero (crea .env desde .env.example)."
        return 1
    fi

    mkdir -p "$LOG_DIR" "$RUN_DIR"
    rm -f "$STOP_FLAG"

    # Android agresivamente suspende/mata procesos de fondo cuando la
    # pantalla se apaga (Doze, "phantom process killer") - sin un wake
    # lock, el dashboard/motor corriendo en Termux puede morir en cuanto
    # el telefono queda inactivo. termux-wake-lock (viene con Termux, no
    # necesita termux-api) lo evita; se libera en ./run.sh --stop.
    if [ "$(detect_platform)" = "termux" ] && command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock 2>/dev/null || true
        log "Wake lock de Termux adquirido (evita que Android suspenda el servidor con la pantalla apagada)."
        log "Consejo: exclui Termux de la optimizacion de bateria de Android para maxima estabilidad (ver README)."
    fi

    log "Arrancando el servidor (bridge + dashboard) en segundo plano..."
    # $0 en vez de una ruta absoluta rompe aca si el script se invoco sin
    # "./" (ej. `bash run.sh --start`) o via un symlink/PATH lookup: $0 queda
    # como "run.sh" a secas y nohup lo busca en $PATH en vez de en
    # PROJECT_ROOT, fallando con "No existe el fichero o el directorio".
    # PROJECT_ROOT/run.sh es siempre valido, sin importar como se llamo.
    RUN_SH_FOREGROUND=1 nohup "$PROJECT_ROOT/run.sh" --start >>"$LOG_DIR/run.log" 2>&1 &
    disown
    echo $! > "$RUN_DIR/supervisor.pid"
    sleep 2

    if kill -0 "$(cat "$RUN_DIR/supervisor.pid" 2>/dev/null)" 2>/dev/null; then
        log "Arrancado. PID del supervisor: $(cat "$RUN_DIR/supervisor.pid")"
        log "Dashboard: http://${DASHBOARD_HOST}:$(dashboard_port) -- iniciá el motor desde ahí cuando estés listo."
        log "Corré ./run.sh --status en unos segundos para confirmar que todo esta arriba."
    else
        err "El supervisor no siguio corriendo. Revisa $LOG_DIR/run.log"
        return 1
    fi
}

# Kills whatever PID is recorded in a pidfile, if it's still alive. Used
# by cleanup() below (stop everything) - only ever invoked via
# `trap cleanup INT TERM EXIT` in cmd_start_foreground, never called
# directly, which is exactly what makes shellcheck's static reachability
# analysis flag both of these as SC2317 "unreachable" (it doesn't treat a
# trap's function-name argument as a call site) - false positive, real
# runtime behavior is unaffected. See shellcheck.net/wiki/SC2317.
# shellcheck disable=SC2317
kill_pidfile() {
    local pidfile="$1"
    [ -f "$pidfile" ] || return 0
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        sleep 1
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
}

# shellcheck disable=SC2317  # only reachable via `trap cleanup ...` - see kill_pidfile's comment above
cleanup() {
    log "Deteniendo procesos del servidor..."
    # STOP_FLAG is intentionally left in place here (only cleared at the
    # top of the NEXT --start) - a supervise() loop interrupted mid-wait
    # by this same signal could otherwise resume and spawn one more
    # "restart" right as we're trying to shut down.
    touch "$STOP_FLAG"
    kill_pidfile "$RUN_DIR/bridge.pid"
    kill_pidfile "$RUN_DIR/dashboard.pid"
    kill_pidfile "$RUN_DIR/xvfb.pid"
    rm -f "$RUN_DIR/supervisor.pid"
    log "Servidor detenido. (El motor, si estaba corriendo, se detiene por separado - ./run.sh --stop se encarga de ambos.)"
}

# Restarts a command forever with capped exponential backoff, writing its
# PID to a pidfile, until $STOP_FLAG exists. A run that survives >= 60s
# resets the backoff, so a process that's generally healthy but flaps
# occasionally isn't punished with an ever-growing wait after one
# transient failure.
supervise() {
    local name="$1" pidfile="$2" logfile="$3"
    shift 3
    local backoff=2
    while [ ! -f "$STOP_FLAG" ]; do
        local start_ts
        start_ts=$(date +%s)
        log "Iniciando $name..."
        "$@" >>"$logfile" 2>&1 &
        local pid=$!
        echo "$pid" > "$pidfile"
        wait "$pid"
        local code=$?
        [ -f "$STOP_FLAG" ] && break

        local ran_for=$(( $(date +%s) - start_ts ))
        if [ "$ran_for" -ge 60 ]; then
            backoff=2
        fi
        err "$name se detuvo (codigo $code) tras ${ran_for}s. Reintentando en ${backoff}s..."
        sleep "$backoff"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt 60 ] && backoff=60
    done
}

# Runs INSIDE the backgrounded process nohup'd by cmd_start above
# (RUN_SH_FOREGROUND=1 is the marker) - actually brings up the bridge and
# the dashboard, and stays alive supervising them until stopped.
cmd_start_foreground() {
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    trap cleanup INT TERM EXIT

    BROKER_KIND="$(grep -E '^BROKER_KIND=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    if [ "${BROKER_KIND:-mt5_bridge}" = "oanda" ]; then
        # Direct REST broker: no bridge process exists in this mode, on any
        # platform - the engine talks HTTPS to OANDA by itself (core/oanda.py).
        log "BROKER_KIND=oanda: sin bridge MT5 que levantar (conexion directa por HTTPS) - arrancando solo el dashboard."
    elif MT5_INSTALL="$(detect_mt5_install)"; then
        # ---- local bridge: an MT5 install exists on this machine - the
        # project's own prefix OR a system one (e.g. the `metatrader`
        # launcher). The bridge MUST run in the SAME prefix as the terminal
        # (the MetaTrader5 package talks to it over intra-prefix IPC), so
        # everything below uses the DETECTED prefix, not a hardcoded one.
        MT5_PREFIX="$(printf '%s' "$MT5_INSTALL" | cut -f1)"
        MT5_TERMINAL_UNIX="$(printf '%s' "$MT5_INSTALL" | cut -f2)"
        MT5_TERMINAL_WIN="$(wine_path_to_windows "$MT5_PREFIX" "$MT5_TERMINAL_UNIX")"
        if [ "$MT5_PREFIX" != "$WINEPREFIX_DIR" ]; then
            log "Usando la instalacion MT5 existente del sistema: $MT5_TERMINAL_UNIX"
            log "(prefijo Wine: $MT5_PREFIX - detectado automaticamente; anulable con MT5_WINEPREFIX/MT5_TERMINAL_PATH en .env)"
        fi

        if [ -z "${DISPLAY:-}" ] && command -v Xvfb >/dev/null 2>&1; then
            log "No hay DISPLAY, iniciando Xvfb en :99"
            Xvfb :99 -screen 0 1024x768x16 >"$LOG_DIR/xvfb.log" 2>&1 &
            echo $! > "$RUN_DIR/xvfb.pid"
            export DISPLAY=:99
            sleep 2
        fi

        WIN_PYTHON="$MT5_PREFIX/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
        if [ ! -f "$WIN_PYTHON" ]; then
            err "No se encontro python de Windows en $WIN_PYTHON (dentro del prefijo del MT5 detectado)."
            err "Corre ./install.sh - instala el Python de Windows y el paquete MetaTrader5 en ESE prefijo. Sigo arrancando el dashboard igual."
        else
            export WINEPREFIX="$MT5_PREFIX"
            export WINEDEBUG=-all

            BRIDGE_AUTH_TOKEN="$(grep -E '^BRIDGE_AUTH_TOKEN=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
            if [ -z "$BRIDGE_AUTH_TOKEN" ]; then
                err "BRIDGE_AUTH_TOKEN no esta en .env - corre ./install.sh para generarlo (el bridge arrancara sin autenticacion mientras tanto)."
            fi

            # Where the bridge LISTENS. 127.0.0.1 (default) = this machine
            # only - a Termux phone using this machine as its remote bridge
            # can NOT connect unless this is widened to 0.0.0.0 (all
            # interfaces) in .env. The bridge server itself enforces the
            # auth token on every request either way (defense in depth for
            # exactly this widening - see bridge/mt5_bridge_server.py).
            BRIDGE_BIND_HOST="$(grep -E '^BRIDGE_BIND_HOST=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
            BRIDGE_BIND_HOST="${BRIDGE_BIND_HOST:-127.0.0.1}"
            if [ "$BRIDGE_BIND_HOST" != "127.0.0.1" ] && [ -z "$BRIDGE_AUTH_TOKEN" ]; then
                err "BRIDGE_BIND_HOST=$BRIDGE_BIND_HOST expone el bridge a la red SIN token de autenticacion."
                err "Cualquiera en esa red podria operar la cuenta. Corre ./install.sh para generar BRIDGE_AUTH_TOKEN antes de abrir el binding."
            fi

            supervise "bridge MT5" "$RUN_DIR/bridge.pid" "$LOG_DIR/bridge.log" \
                wine "$WIN_PYTHON" "$PROJECT_ROOT/bridge/mt5_bridge_server.py" --port 5001 --host "$BRIDGE_BIND_HOST" --token "$BRIDGE_AUTH_TOKEN" --terminal-path "$MT5_TERMINAL_WIN" &
            disown

            # Health-check target: with 0.0.0.0 (all interfaces) or the
            # 127.0.0.1 default, loopback works; with a specific LAN IP
            # bind, loopback does NOT - poll the actual bound IP.
            BRIDGE_HEALTH_HOST="$BRIDGE_BIND_HOST"
            [ "$BRIDGE_HEALTH_HOST" = "0.0.0.0" ] && BRIDGE_HEALTH_HOST="127.0.0.1"
            log "Esperando a que el bridge responda en http://${BRIDGE_HEALTH_HOST}:5001/health ..."
            ready=0
            for _ in $(seq 1 60); do
                if curl -fsS "http://${BRIDGE_HEALTH_HOST}:5001/health" >/dev/null 2>&1; then
                    ready=1
                    break
                fi
                sleep 2
            done
            if [ "$ready" -ne 1 ]; then
                # Deliberately NOT fatal: the dashboard still starts below,
                # so credentials/config can be fixed from its Settings tab
                # instead of only from a terminal. ./run.sh doctor and
                # --status both surface this clearly either way.
                err "El bridge no respondio a tiempo. Revisa $LOG_DIR/bridge.log - arrancando el dashboard igual."
            else
                log "Bridge listo."
            fi
        fi
    else
        # ---- no MT5 install anywhere on this machine (e.g. Termux): remote bridge ----
        MT5_BRIDGE_URL="$(grep -E '^MT5_BRIDGE_URL=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
        MT5_BRIDGE_URL="${MT5_BRIDGE_URL:-http://127.0.0.1:5001}"
        log "No se encontro ninguna instalacion MT5 local (ni $WINEPREFIX_DIR, ni comando 'metatrader', ni ~/.wine|~/.mt5) - asumiendo bridge remoto en $MT5_BRIDGE_URL"
        if curl -fsS "${MT5_BRIDGE_URL}/health" >/dev/null 2>&1; then
            log "Bridge remoto responde."
        else
            err "No se pudo conectar a ${MT5_BRIDGE_URL}/health todavia."
            err "Si el bridge corre en otra maquina (Kali/Ubuntu con ./install.sh corrido), confirma que este levantado (./run.sh --start ahi) y que MT5_BRIDGE_URL en .env aca sea correcto."
            err "Arrancando el dashboard igual - podes ajustar MT5_BRIDGE_URL desde su pestaña Settings."
        fi
    fi

    supervise "dashboard" "$RUN_DIR/dashboard.pid" "$LOG_DIR/dashboard.log" \
        python3 "$PROJECT_ROOT/dashboard.py" --web --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT"
}

# ======================================================= subcommand dispatch
case "${1:-}" in
    --start)
        if [ "${RUN_SH_FOREGROUND:-0}" = "1" ]; then
            cmd_start_foreground
        else
            cmd_start
        fi
        exit $?
        ;;
    --stop)
        cmd_stop_all; exit $?
        ;;
    --status)
        cmd_status; exit $?
        ;;
    emergency-stop)
        shift
        cmd_emergency_stop "$@"; exit $?
        ;;
    verify)
        cmd_verify; exit $?
        ;;
    doctor)
        cmd_doctor; exit $?
        ;;
    *)
        cat <<EOF
Uso: ./run.sh <comando>

  --start              arranca el servidor (bridge + dashboard) en segundo plano
  --stop               detiene TODO (motor + dashboard + bridge) - no queda nada corriendo
  --status             estado actual, bien formateado

  emergency-stop [--clear]   interruptor de emergencia manual
  verify                     compila + corre tests + prueba de humo + chequeo del dashboard
  doctor                     diagnostico completo de la instalacion

El motor de trading (el bot en si) se inicia y se detiene DESDE EL DASHBOARD
(http://127.0.0.1:9000 despues de --start), no con este script - ver el
encabezado de este archivo para el porque.
EOF
        exit 1
        ;;
esac
