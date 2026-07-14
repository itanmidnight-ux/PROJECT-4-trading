#!/usr/bin/env bash
# =====================================================================
# run.sh - single entrypoint that starts everything the trading engine
# needs: a virtual display (if none), the Wine-side MT5 bridge, and the
# engine itself. Run install.sh once before this.
#
# Both the bridge and the engine are supervised: if either process dies
# unexpectedly (bridge crash, Wine hiccup, uncaught exception) it is
# restarted automatically with exponential backoff, instead of taking
# the whole system down. Ctrl+C (or `./stop.sh` in --daemon mode) stops
# everything cleanly.
#
# Usage:
#   ./run.sh                 run in the foreground
#   ./run.sh --synthetic     no broker connection, simulated prices (testing)
#   ./run.sh --daemon        run detached in the background (see ./stop.sh)
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
mkdir -p "$LOG_DIR" "$RUN_DIR"
rm -f "$STOP_FLAG"

log()  { printf '\033[1;36m[run]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[run][error]\033[0m %s\n' "$*" >&2; }

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
        err "Ya hay una instancia corriendo (PID $(cat "$RUN_DIR/supervisor.pid")). Usa ./stop.sh primero."
        exit 1
    fi
    log "Arrancando en modo daemon. Logs en $LOG_DIR, control con ./stop.sh"
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

# shellcheck disable=SC1090
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
    # stop.sh would then have no way to reach.
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

if [ -z "$SYNTHETIC_FLAG" ]; then
    # ---- virtual display for Wine, if none is already available ----
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
fi

supervise "motor de trading" "$RUN_DIR/engine.pid" "$LOG_DIR/engine.stdout.log" \
    python3 "$PROJECT_ROOT/main.py" $SYNTHETIC_FLAG
