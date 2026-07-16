#!/usr/bin/env bash
# =====================================================================
# run.sh - the only other entrypoint besides install.sh. Auto-detects the
# platform (Kali/Ubuntu/Termux/other) the same way install.sh does, and
# adjusts what it does: on a machine with a local Wine prefix (set up by
# install.sh's Kali/Ubuntu path) it starts a local MT5 bridge; without one
# (Termux, or any install that skipped Wine) it talks to whatever
# MT5_BRIDGE_URL points at instead of assuming a local bridge exists.
#
# Subcommands (this file replaces stop.sh, emergency_stop.sh,
# scripts/verify.sh and scripts/doctor.sh - install.sh and this are the
# only two .sh files in the project):
#
#   ./run.sh                 start in the foreground
#   ./run.sh --synthetic     no broker connection, simulated prices (testing)
#   ./run.sh --daemon        start detached in the background
#   ./run.sh stop            stop a --daemon instance cleanly
#   ./run.sh emergency-stop [--clear]   manual kill switch (see below)
#   ./run.sh verify          compile + tests + synthetic smoke test + dashboard API check
#   ./run.sh doctor          read-only diagnostic of the current install
#
# Both the bridge and the engine (when started normally) are supervised:
# if either process dies unexpectedly it is restarted automatically with
# exponential backoff, instead of taking the whole system down. Ctrl+C
# (or `./run.sh stop` in --daemon mode) stops everything cleanly.
#
# The dashboard is separate on purpose (run `python dashboard.py` in
# another terminal) so you can watch stats without it dying if you
# restart the engine, and vice versa.
# =====================================================================
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT" || exit 1

VENV_DIR="$PROJECT_ROOT/.venv"
WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
LOG_DIR="$PROJECT_ROOT/data/logs"
RUN_DIR="$PROJECT_ROOT/data/run"
STOP_FLAG="$RUN_DIR/stop.flag"

log()  { printf '\033[1;36m[run]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[run][error]\033[0m %s\n' "$*" >&2; }

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

# ============================================================ cmd: stop
cmd_stop() {
    local pidfile="$RUN_DIR/supervisor.pid"
    if [ ! -f "$pidfile" ]; then
        err "No hay una instancia registrada en $pidfile (no esta corriendo, o no se inicio con --daemon)."
        return 1
    fi
    local pid
    pid=$(cat "$pidfile")
    if ! kill -0 "$pid" 2>/dev/null; then
        err "El proceso $pid ya no existe. Limpiando pidfile."
        rm -f "$pidfile"
        return 0
    fi
    log "Enviando señal de parada al supervisor (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || { log "Detenido."; return 0; }
        sleep 1
    done
    err "No se detuvo a tiempo, forzando con SIGKILL."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$pidfile"
}

# =================================================== cmd: emergency-stop
# Manual kill switch: touches the file the engine checks every poll cycle
# (see core/engine.py, KILL_SWITCH_PATH in .env). The engine force-closes
# any open position at market on its next step and halts - it does NOT
# rely on the engine process being reachable via `run.sh stop`/kill, only
# on the filesystem being writable, so this also works if something (Wine
# hang, bridge stuck) is preventing a clean process shutdown.
cmd_emergency_stop() {
    local kill_switch_path flag run_stop_flag
    kill_switch_path="$(grep -E '^KILL_SWITCH_PATH=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    kill_switch_path="${kill_switch_path:-data/EMERGENCY_STOP}"
    flag="$PROJECT_ROOT/$kill_switch_path"
    run_stop_flag="$PROJECT_ROOT/data/run/stop.flag"

    if [ "${1:-}" = "--clear" ]; then
        rm -f "$flag" "$run_stop_flag"
        log "Interruptor de emergencia desactivado ($flag eliminado)."
        log "Corre ./run.sh de nuevo para reanudar el trading."
        return 0
    fi

    mkdir -p "$(dirname "$flag")"
    touch "$flag"
    log "ACTIVADO: $flag creado."
    log "El motor cerrara cualquier posicion abierta a mercado y se detendra en su proximo ciclo de sondeo."
    log "Para reanudar despues: ./run.sh emergency-stop --clear && ./run.sh"
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
 Para eso: ./install.sh, luego ./run.sh y revisa data/logs/bridge.log.
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
            if .venv/bin/python3 -c "import pandas, numpy, flask, requests, dotenv" 2>/dev/null; then
                ok "dependencias (sin pywebview - no aplica en Termux) importan correctamente"
            else
                bad "faltan dependencias en .venv - corre ./install.sh"
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
        if [ -n "${MT5_LOGIN:-}" ] && [ -n "${MT5_PASSWORD:-}" ]; then
            ok "MT5_LOGIN y MT5_PASSWORD estan configurados"
        else
            bad "MT5_LOGIN o MT5_PASSWORD vacios en .env - editalo o vuelve a correr ./install.sh"
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
            warn_msg "El bot esta PAUSADO manualmente ($pause_flag existe) - no abrira operaciones nuevas hasta \"Reanudar bot\" en el dashboard."
        else
            ok "Bot no pausado ($pause_flag no existe)"
        fi
        if [ -n "${DASHBOARD_AUTH_TOKEN:-}" ]; then
            ok "DASHBOARD_AUTH_TOKEN configurado (Settings y pausar/reanudar exigen autenticacion)"
        else
            warn_msg "DASHBOARD_AUTH_TOKEN vacio - sin importancia en uso local; configuralo si vas a usar --web --host 0.0.0.0."
        fi
    else
        bad "no existe .env - corre ./install.sh"
    fi

    if [ "$platform" = "termux" ]; then
        section "3. Bridge remoto (Termux no corre Wine/MT5 local)"
        local bridge_url="${MT5_BRIDGE_URL:-http://127.0.0.1:5001}"
        case "$bridge_url" in
            *127.0.0.1*|*localhost*)
                warn_msg "MT5_BRIDGE_URL ($bridge_url) apunta a esta misma maquina, pero Termux no corre un bridge local - configuralo con la IP de una Kali/Ubuntu real que si lo tenga."
                ;;
            *)
                ok "MT5_BRIDGE_URL configurado como remoto: $bridge_url"
                if curl -fsS "${bridge_url}/health" >/dev/null 2>&1; then
                    ok "el bridge remoto responde"
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

        if [ -d "$WINEPREFIX_DIR" ]; then
            ok "prefijo de Wine existe en $WINEPREFIX_DIR"
        else
            bad "no existe el prefijo de Wine ($WINEPREFIX_DIR) - corre ./install.sh"
        fi

        local mt5_marker="$WINEPREFIX_DIR/drive_c/Program Files/MetaTrader 5/terminal64.exe"
        if [ -f "$mt5_marker" ]; then
            ok "terminal MetaTrader 5 instalado"
        else
            bad "no se encontro terminal64.exe - corre ./install.sh"
        fi

        local win_python
        win_python="$WINEPREFIX_DIR/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
        if [ -f "$win_python" ]; then
            ok "python de Windows (para el bridge) instalado"
            export WINEPREFIX="$WINEPREFIX_DIR"
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

    section "5. Bridge MT5 (proceso en vivo, local)"
    if curl -fsS "http://127.0.0.1:5001/health" >/dev/null 2>&1; then
        local health_json
        health_json=$(curl -fsS "http://127.0.0.1:5001/health" 2>/dev/null)
        if echo "$health_json" | grep -q '"connected": *true'; then
            ok "bridge corriendo y con sesion MT5 activa"
        else
            warn_msg "bridge corriendo pero SIN sesion MT5 activa (falta /login o credenciales invalidas)"
        fi
    else
        warn_msg "bridge no responde en 127.0.0.1:5001 (normal si ./run.sh no esta corriendo ahora mismo, o si el bridge es remoto)"
    fi

    section "6. Procesos y espacio en disco"
    if [ -f "data/run/supervisor.pid" ] && kill -0 "$(cat data/run/supervisor.pid)" 2>/dev/null; then
        ok "hay una instancia de run.sh corriendo (PID $(cat data/run/supervisor.pid))"
    else
        warn_msg "no hay ninguna instancia de run.sh corriendo ahora mismo"
    fi

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

# ======================================================= subcommand dispatch
SUBCOMMAND="${1:-}"
case "$SUBCOMMAND" in
    stop)
        cmd_stop; exit $?
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
esac

# ============================================================= start (default)
mkdir -p "$LOG_DIR" "$RUN_DIR"
rm -f "$STOP_FLAG"

SYNTHETIC_FLAG=""
DAEMON=0
for arg in "$@"; do
    case "$arg" in
        --synthetic) SYNTHETIC_FLAG="--synthetic" ;;
        --daemon) DAEMON=1 ;;
    esac
done

# ------------------------------------------------------------ daemon mode
if [ "$DAEMON" -eq 1 ]; then
    if [ -f "$RUN_DIR/supervisor.pid" ] && kill -0 "$(cat "$RUN_DIR/supervisor.pid")" 2>/dev/null; then
        err "Ya hay una instancia corriendo (PID $(cat "$RUN_DIR/supervisor.pid")). Usa ./run.sh stop primero."
        exit 1
    fi
    log "Arrancando en modo daemon. Logs en $LOG_DIR, control con ./run.sh stop"
    nohup "$0" $SYNTHETIC_FLAG >>"$LOG_DIR/run.log" 2>&1 &
    disown
    echo $! > "$RUN_DIR/supervisor.pid"
    sleep 1
    log "PID del supervisor: $(cat "$RUN_DIR/supervisor.pid")"
    exit 0
fi

echo $$ > "$RUN_DIR/supervisor.pid"

if [ ! -d "$VENV_DIR" ]; then
    err "No se encontro $VENV_DIR. Corre ./install.sh primero."
    exit 1
fi
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    err "No se encontro .env. Corre ./install.sh primero (crea .env desde .env.example)."
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Kills whatever PID is recorded in a pidfile, if it's still alive. Used
# both by cleanup (stop everything) and by supervise (stop a superseded
# child before restarting it), so a dead process never leaves the pidfile
# pointing at something misleading.
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

cleanup() {
    log "Deteniendo procesos..."
    # STOP_FLAG is intentionally left in place here (only cleared at the
    # top of the NEXT ./run.sh invocation): if we deleted it now, a
    # supervise() loop that gets interrupted mid-wait by this same signal
    # could resume, find the flag already gone, and spawn one more
    # "restart" right as we're trying to shut down - an orphaned process
    # `./run.sh stop` would then have no way to reach.
    touch "$STOP_FLAG"
    kill_pidfile "$RUN_DIR/engine.pid"
    kill_pidfile "$RUN_DIR/bridge.pid"
    kill_pidfile "$RUN_DIR/xvfb.pid"
    rm -f "$RUN_DIR/supervisor.pid"
    log "Detenido."
}
trap cleanup INT TERM EXIT

# Restarts a command forever with capped exponential backoff, writing its
# PID to a pidfile, until $STOP_FLAG exists. A stop signal is a FILE (not
# a shell variable) because this function's main loop runs in the current
# process but the command it launches is a separate one - a file is the
# one thing both the trap (in this process) and a future re-check here
# can see. A run that survives >60s resets the backoff, so a process
# that's generally healthy but flaps occasionally isn't punished with an
# ever-growing wait after one transient failure.
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

if [ -z "$SYNTHETIC_FLAG" ] && [ -d "$WINEPREFIX_DIR" ]; then
    # ---- local bridge: this machine has a Wine prefix (Kali/Ubuntu install) ----
    if [ -z "${DISPLAY:-}" ] && command -v Xvfb >/dev/null 2>&1; then
        log "No hay DISPLAY, iniciando Xvfb en :99"
        Xvfb :99 -screen 0 1024x768x16 >"$LOG_DIR/xvfb.log" 2>&1 &
        echo $! > "$RUN_DIR/xvfb.pid"
        export DISPLAY=:99
        sleep 2
    fi

    WIN_PYTHON="$WINEPREFIX_DIR/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
    if [ ! -f "$WIN_PYTHON" ]; then
        err "No se encontro python de Windows en $WIN_PYTHON. Corre ./install.sh."
        exit 1
    fi
    export WINEPREFIX="$WINEPREFIX_DIR"
    export WINEDEBUG=-all

    BRIDGE_AUTH_TOKEN="$(grep -E '^BRIDGE_AUTH_TOKEN=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    if [ -z "$BRIDGE_AUTH_TOKEN" ]; then
        err "BRIDGE_AUTH_TOKEN no esta en .env - corre ./install.sh para generarlo (el bridge arrancara sin autenticacion mientras tanto)."
    fi

    supervise "bridge MT5" "$RUN_DIR/bridge.pid" "$LOG_DIR/bridge.log" \
        wine "$WIN_PYTHON" "$PROJECT_ROOT/bridge/mt5_bridge_server.py" --port 5001 --token "$BRIDGE_AUTH_TOKEN" &
    disown

    log "Esperando a que el bridge responda en http://127.0.0.1:5001/health ..."
    ready=0
    for _ in $(seq 1 60); do
        if curl -fsS "http://127.0.0.1:5001/health" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 2
    done
    if [ "$ready" -ne 1 ]; then
        err "El bridge no respondio a tiempo. Revisa $LOG_DIR/bridge.log"
        cleanup
        exit 1
    fi
    log "Bridge listo."
elif [ -z "$SYNTHETIC_FLAG" ]; then
    # ---- no local Wine prefix (e.g. Termux): assume a remote bridge ----
    MT5_BRIDGE_URL="$(grep -E '^MT5_BRIDGE_URL=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    MT5_BRIDGE_URL="${MT5_BRIDGE_URL:-http://127.0.0.1:5001}"
    log "No hay un prefijo de Wine local ($WINEPREFIX_DIR) - asumiendo bridge remoto en $MT5_BRIDGE_URL"
    if curl -fsS "${MT5_BRIDGE_URL}/health" >/dev/null 2>&1; then
        log "Bridge remoto responde."
    else
        err "No se pudo conectar a ${MT5_BRIDGE_URL}/health."
        err "Si el bridge corre en otra maquina (Kali/Ubuntu con ./install.sh corrido), confirma que este levantado (./run.sh ahi) y que MT5_BRIDGE_URL en .env aca sea correcto."
        err "Para probar sin broker mientras tanto: ./run.sh --synthetic"
        cleanup
        exit 1
    fi
fi

supervise "motor de trading" "$RUN_DIR/engine.pid" "$LOG_DIR/engine.stdout.log" \
    python3 "$PROJECT_ROOT/main.py" $SYNTHETIC_FLAG
